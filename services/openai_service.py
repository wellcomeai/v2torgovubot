"""
Trading Bot OpenAI Service  
AI-powered анализ торговых сигналов и рыночных данных
"""

import asyncio
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Union, Tuple
from dataclasses import dataclass, field
from enum import Enum
import hashlib
from concurrent.futures import ThreadPoolExecutor
import aiohttp

from utils.logger import setup_logger
from utils.helpers import (
    get_current_timestamp, safe_float, Timer, retry_async
)
from app.config.settings import Settings, get_settings
from data.database import Database
from data.historical_data import CandleData
from strategies.base_strategy import StrategyResult, SignalType


# ============================================================================
# CONSTANTS AND ENUMS
# ============================================================================

logger = setup_logger(__name__)

# API настройки
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4"
DEFAULT_MAX_TOKENS = 1000
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT = 30

# Лимиты и кеширование
MAX_PROMPT_LENGTH = 4000  # Максимальная длина промпта
CACHE_TTL_SECONDS = 3600  # 1 час кеша для похожих запросов
MAX_RETRIES = 3
RETRY_DELAY = 2

# Rate limiting (OpenAI limits)
REQUESTS_PER_MINUTE = 50
REQUESTS_PER_DAY = 1000


class AnalysisType(str, Enum):
    """Типы AI анализа"""
    TECHNICAL = "technical"           # Технический анализ
    SENTIMENT = "sentiment"           # Анализ настроений
    FUNDAMENTAL = "fundamental"       # Фундаментальный анализ
    RISK_ASSESSMENT = "risk"          # Оценка рисков
    MARKET_OVERVIEW = "market"        # Обзор рынка
    STRATEGY_VALIDATION = "strategy"  # Валидация стратегии


class AnalysisConfidence(str, Enum):
    """Уровни уверенности AI анализа"""
    VERY_LOW = "very_low"      # 0.0-0.3
    LOW = "low"                # 0.3-0.5
    MODERATE = "moderate"      # 0.5-0.7
    HIGH = "high"              # 0.7-0.9
    VERY_HIGH = "very_high"    # 0.9-1.0


@dataclass
class AIAnalysisRequest:
    """Запрос на AI анализ"""
    analysis_type: AnalysisType
    symbol: str
    timeframe: str
    market_data: Optional[List[CandleData]] = None
    signal_data: Optional[StrategyResult] = None
    additional_context: Dict[str, Any] = field(default_factory=dict)
    custom_prompt: Optional[str] = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE


@dataclass
class AIAnalysisResponse:
    """Результат AI анализа"""
    analysis_type: AnalysisType
    symbol: str
    analysis_text: str
    confidence: float
    confidence_level: AnalysisConfidence
    key_points: List[str]
    recommendations: List[str]
    risk_factors: List[str]
    timestamp: int = field(default_factory=get_current_timestamp)
    
    # Метаданные
    model_used: str = ""
    tokens_used: int = 0
    processing_time: float = 0.0
    cached: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Конвертация в словарь"""
        return {
            'analysis_type': self.analysis_type.value,
            'symbol': self.symbol,
            'analysis_text': self.analysis_text,
            'confidence': self.confidence,
            'confidence_level': self.confidence_level.value,
            'key_points': self.key_points,
            'recommendations': self.recommendations,
            'risk_factors': self.risk_factors,
            'timestamp': self.timestamp,
            'model_used': self.model_used,
            'tokens_used': self.tokens_used,
            'processing_time': self.processing_time,
            'cached': self.cached
        }


# ============================================================================
# OPENAI SERVICE CLASS
# ============================================================================

class OpenAIService:
    """
    Сервис для AI-анализа торговых данных с помощью OpenAI API
    
    Возможности:
    - Технический анализ рыночных данных
    - Анализ торговых сигналов
    - Генерация торговых рекомендаций
    - Оценка рисков
    - Кеширование результатов
    - Rate limiting
    """
    
    def __init__(
        self,
        settings: Optional[Settings] = None,
        database: Optional[Database] = None
    ):
        self.settings = settings or get_settings()
        self.database = database
        self.logger = setup_logger(f"{__name__}.OpenAIService")
        
        # API настройки
        self.api_key = self.settings.OPENAI_API_KEY
        self.model = self.settings.OPENAI_MODEL
        self.max_tokens = self.settings.OPENAI_MAX_TOKENS
        self.temperature = self.settings.OPENAI_TEMPERATURE
        self.timeout = self.settings.OPENAI_TIMEOUT
        
        # HTTP сессия
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Rate limiting
        self._request_times = []
        self._daily_requests = 0
        self._last_reset_date = datetime.now(timezone.utc).date()
        
        # Кеш анализов
        self._analysis_cache: Dict[str, AIAnalysisResponse] = {}
        self._cache_timestamps: Dict[str, int] = {}
        
        # Executor для CPU-intensive операций
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="OpenAI")
        
        # Статистика
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'cache_hits': 0,
            'total_tokens_used': 0,
            'avg_response_time': 0.0
        }
        
        if not self.api_key:
            self.logger.warning("⚠️ OpenAI API key not configured")
        else:
            self.logger.info("🤖 OpenAI Service initialized")
    
    async def __aenter__(self):
        """Async context manager entry"""
        await self._create_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
    
    async def _create_session(self):
        """Создание HTTP сессии"""
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'Content-Type': 'application/json',
                    'User-Agent': 'TradingBot/1.0.0'
                }
            )
    
    async def close(self):
        """Закрытие ресурсов"""
        if self._session:
            await self._session.close()
            self._session = None
        
        self._executor.shutdown(wait=True)
        self.logger.info("🤖 OpenAI Service closed")
    
    # ========================================================================
    # PUBLIC API METHODS
    # ========================================================================
    
    async def analyze_signal(
        self,
        signal: StrategyResult,
        symbol: str,
        timeframe: str,
        market_data: Optional[List[CandleData]] = None,
        additional_context: Optional[Dict[str, Any]] = None
    ) -> Optional[AIAnalysisResponse]:
        """
        Анализ торгового сигнала с помощью AI
        
        Args:
            signal: Торговый сигнал от стратегии
            symbol: Торговая пара
            timeframe: Таймфрейм
            market_data: Рыночные данные для контекста
            additional_context: Дополнительный контекст
            
        Returns:
            Результат AI анализа или None при ошибке
        """
        if not self.api_key:
            self.logger.warning("⚠️ OpenAI API key not configured")
            return None
        
        try:
            request = AIAnalysisRequest(
                analysis_type=AnalysisType.STRATEGY_VALIDATION,
                symbol=symbol,
                timeframe=timeframe,
                market_data=market_data,
                signal_data=signal,
                additional_context=additional_context or {}
            )
            
            return await self._process_analysis_request(request)
            
        except Exception as e:
            self.logger.error(f"❌ Signal analysis failed: {e}")
            return None
    
    async def analyze_market_data(
        self,
        symbol: str,
        timeframe: str,
        market_data: List[CandleData],
        analysis_type: AnalysisType = AnalysisType.TECHNICAL,
        additional_context: Optional[Dict[str, Any]] = None
    ) -> Optional[AIAnalysisResponse]:
        """
        Технический анализ рыночных данных
        
        Args:
            symbol: Торговая пара
            timeframe: Таймфрейм
            market_data: Рыночные данные
            analysis_type: Тип анализа
            additional_context: Дополнительный контекст
            
        Returns:
            Результат AI анализа
        """
        if not self.api_key:
            return None
        
        try:
            request = AIAnalysisRequest(
                analysis_type=analysis_type,
                symbol=symbol,
                timeframe=timeframe,
                market_data=market_data,
                additional_context=additional_context or {}
            )
            
            return await self._process_analysis_request(request)
            
        except Exception as e:
            self.logger.error(f"❌ Market data analysis failed: {e}")
            return None
    
    async def get_trading_recommendation(
        self,
        symbol: str,
        current_price: float,
        market_data: Optional[List[CandleData]] = None,
        portfolio_context: Optional[Dict[str, Any]] = None
    ) -> Optional[AIAnalysisResponse]:
        """
        Получение торговых рекомендаций
        
        Args:
            symbol: Торговая пара
            current_price: Текущая цена
            market_data: Рыночные данные
            portfolio_context: Контекст портфеля
            
        Returns:
            Торговые рекомендации
        """
        if not self.api_key:
            return None
        
        try:
            context = portfolio_context or {}
            context.update({
                'current_price': current_price,
                'analysis_focus': 'trading_recommendation'
            })
            
            request = AIAnalysisRequest(
                analysis_type=AnalysisType.TECHNICAL,
                symbol=symbol,
                timeframe="1h",  # Default для рекомендаций
                market_data=market_data,
                additional_context=context,
                custom_prompt=self._get_recommendation_prompt()
            )
            
            return await self._process_analysis_request(request)
            
        except Exception as e:
            self.logger.error(f"❌ Trading recommendation failed: {e}")
            return None
    
    async def assess_risk(
        self,
        symbol: str,
        signal: Optional[StrategyResult] = None,
        market_data: Optional[List[CandleData]] = None,
        portfolio_context: Optional[Dict[str, Any]] = None
    ) -> Optional[AIAnalysisResponse]:
        """
        Оценка рисков торговой операции
        
        Args:
            symbol: Торговая пара
            signal: Торговый сигнал (опционально)
            market_data: Рыночные данные
            portfolio_context: Контекст портфеля
            
        Returns:
            Анализ рисков
        """
        if not self.api_key:
            return None
        
        try:
            request = AIAnalysisRequest(
                analysis_type=AnalysisType.RISK_ASSESSMENT,
                symbol=symbol,
                timeframe="4h",  # Более широкий контекст для оценки рисков
                market_data=market_data,
                signal_data=signal,
                additional_context=portfolio_context or {}
            )
            
            return await self._process_analysis_request(request)
            
        except Exception as e:
            self.logger.error(f"❌ Risk assessment failed: {e}")
            return None
    
    async def analyze_market_sentiment(
        self,
        symbol: str,
        news_data: Optional[List[Dict[str, Any]]] = None,
        social_data: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[AIAnalysisResponse]:
        """
        Анализ рыночных настроений
        
        Args:
            symbol: Торговая пара
            news_data: Новостные данные
            social_data: Данные из соцсетей
            
        Returns:
            Анализ настроений
        """
        if not self.api_key:
            return None
        
        try:
            context = {}
            if news_data:
                context['news'] = news_data[:10]  # Ограничиваем количество
            if social_data:
                context['social'] = social_data[:20]
            
            request = AIAnalysisRequest(
                analysis_type=AnalysisType.SENTIMENT,
                symbol=symbol,
                timeframe="1d",
                additional_context=context
            )
            
            return await self._process_analysis_request(request)
            
        except Exception as e:
            self.logger.error(f"❌ Sentiment analysis failed: {e}")
            return None
    
    # ========================================================================
    # CORE PROCESSING METHODS
    # ========================================================================
    
    async def _process_analysis_request(self, request: AIAnalysisRequest) -> Optional[AIAnalysisResponse]:
        """Обработка запроса на анализ"""
        
        try:
            # Проверяем кеш
            cache_key = self._generate_cache_key(request)
            cached_result = self._get_from_cache(cache_key)
            
            if cached_result:
                self.stats['cache_hits'] += 1
                cached_result.cached = True
                self.logger.debug(f"🎯 Cache hit for {request.analysis_type.value} analysis")
                return cached_result
            
            # Проверяем rate limits
            if not await self._check_rate_limits():
                self.logger.warning("⚠️ Rate limit exceeded")
                return None
            
            # Генерируем промпт
            prompt = await self._generate_prompt(request)
            
            if not prompt:
                self.logger.error("❌ Failed to generate prompt")
                return None
            
            # Отправляем запрос к OpenAI
            with Timer(f"openai_request_{request.analysis_type.value}"):
                response = await self._send_openai_request(prompt, request)
            
            if response:
                # Сохраняем в кеш
                self._save_to_cache(cache_key, response)
                
                # Сохраняем в БД если доступна
                if self.database:
                    await self._save_analysis_to_db(request, response)
                
                self.stats['successful_requests'] += 1
                return response
            else:
                self.stats['failed_requests'] += 1
                return None
                
        except Exception as e:
            self.stats['failed_requests'] += 1
            self.logger.error(f"❌ Analysis request processing failed: {e}")
            return None
    
    async def _send_openai_request(
        self, 
        prompt: str, 
        request: AIAnalysisRequest
    ) -> Optional[AIAnalysisResponse]:
        """Отправка запроса к OpenAI API"""
        
        if not self._session:
            await self._create_session()
        
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": self._get_system_prompt(request.analysis_type)
                    },
                    {
                        "role": "user", 
                        "content": prompt
                    }
                ],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "response_format": {"type": "json_object"}  # Запрашиваем JSON ответ
            }
            
            start_time = time.time()
            
            async with self._session.post(OPENAI_API_URL, json=payload) as response:
                
                processing_time = time.time() - start_time
                
                if response.status == 200:
                    data = await response.json()
                    return await self._parse_openai_response(data, request, processing_time)
                    
                elif response.status == 429:  # Rate limit
                    self.logger.warning("⚠️ OpenAI rate limit hit")
                    return None
                    
                else:
                    error_text = await response.text()
                    self.logger.error(f"❌ OpenAI API error {response.status}: {error_text}")
                    return None
                    
        except asyncio.TimeoutError:
            self.logger.error("❌ OpenAI request timeout")
            return None
            
        except Exception as e:
            self.logger.error(f"❌ OpenAI request failed: {e}")
            return None
    
    async def _parse_openai_response(
        self, 
        data: Dict[str, Any], 
        request: AIAnalysisRequest,
        processing_time: float
    ) -> Optional[AIAnalysisResponse]:
        """Парсинг ответа от OpenAI"""
        
        try:
            if 'choices' not in data or not data['choices']:
                self.logger.error("❌ No choices in OpenAI response")
                return None
            
            content = data['choices'][0]['message']['content']
            usage = data.get('usage', {})
            tokens_used = usage.get('total_tokens', 0)
            
            # Парсим JSON ответ
            try:
                parsed_content = json.loads(content)
            except json.JSONDecodeError:
                # Fallback если ответ не в JSON формате
                parsed_content = {
                    'analysis': content,
                    'confidence': 0.5,
                    'key_points': [],
                    'recommendations': [],
                    'risk_factors': []
                }
            
            # Определяем уровень уверенности
            confidence = float(parsed_content.get('confidence', 0.5))
            confidence_level = self._determine_confidence_level(confidence)
            
            # Обновляем статистику
            self.stats['total_tokens_used'] += tokens_used
            self.stats['total_requests'] += 1
            
            # Обновляем среднее время ответа
            if self.stats['successful_requests'] > 0:
                self.stats['avg_response_time'] = (
                    (self.stats['avg_response_time'] * (self.stats['successful_requests'] - 1) + processing_time) /
                    self.stats['successful_requests']
                )
            else:
                self.stats['avg_response_time'] = processing_time
            
            response = AIAnalysisResponse(
                analysis_type=request.analysis_type,
                symbol=request.symbol,
                analysis_text=parsed_content.get('analysis', ''),
                confidence=confidence,
                confidence_level=confidence_level,
                key_points=parsed_content.get('key_points', []),
                recommendations=parsed_content.get('recommendations', []),
                risk_factors=parsed_content.get('risk_factors', []),
                model_used=self.model,
                tokens_used=tokens_used,
                processing_time=processing_time
            )
            
            return response
            
        except Exception as e:
            self.logger.error(f"❌ Failed to parse OpenAI response: {e}")
            return None
    
    # ========================================================================
    # PROMPT GENERATION
    # ========================================================================
    
    def _get_system_prompt(self, analysis_type: AnalysisType) -> str:
        """Получение системного промпта для разных типов анализа"""
        
        base_prompt = """You are an expert cryptocurrency trading analyst with deep knowledge of technical analysis, market psychology, and risk management. 

Your responses must be in JSON format with the following structure:
{
  "analysis": "detailed analysis text",
  "confidence": 0.85,
  "key_points": ["point1", "point2", "point3"],
  "recommendations": ["recommendation1", "recommendation2"],
  "risk_factors": ["risk1", "risk2"]
}

Confidence should be a float between 0.0 and 1.0."""
        
        type_specific_prompts = {
            AnalysisType.TECHNICAL: """
Focus on technical analysis including:
- Price action and trend analysis
- Support and resistance levels
- Volume patterns
- Technical indicators interpretation
- Chart patterns recognition
""",
            AnalysisType.STRATEGY_VALIDATION: """
Focus on validating trading signals:
- Signal quality assessment
- Entry/exit point optimization
- Risk-reward ratio evaluation
- Market context appropriateness
- Timing considerations
""",
            AnalysisType.RISK_ASSESSMENT: """
Focus on risk evaluation:
- Position sizing recommendations
- Stop-loss placement
- Market volatility assessment
- Correlation risks
- Maximum drawdown potential
""",
            AnalysisType.SENTIMENT: """
Focus on market sentiment:
- News impact analysis
- Social media sentiment
- Market fear/greed indicators
- Institutional vs retail sentiment
- Contrarian opportunities
""",
            AnalysisType.FUNDAMENTAL: """
Focus on fundamental analysis:
- Project fundamentals
- Token economics
- Development activity
- Adoption metrics
- Competitive positioning
""",
            AnalysisType.MARKET_OVERVIEW: """
Focus on broader market context:
- Overall market conditions
- Sector performance
- Macro economic factors
- Market cycles
- Correlation with traditional assets
"""
        }
        
        return base_prompt + type_specific_prompts.get(analysis_type, "")
    
    async def _generate_prompt(self, request: AIAnalysisRequest) -> Optional[str]:
        """Генерация промпта для анализа"""
        
        try:
            if request.custom_prompt:
                return request.custom_prompt[:MAX_PROMPT_LENGTH]
            
            prompt_parts = []
            
            # Базовая информация
            prompt_parts.append(f"Symbol: {request.symbol}")
            prompt_parts.append(f"Timeframe: {request.timeframe}")
            prompt_parts.append(f"Analysis Type: {request.analysis_type.value}")
            
            # Рыночные данные
            if request.market_data and len(request.market_data) > 0:
                market_summary = await self._summarize_market_data(request.market_data)
                prompt_parts.append(f"Market Data Summary:\n{market_summary}")
            
            # Данные сигнала
            if request.signal_data:
                signal_summary = self._summarize_signal_data(request.signal_data)
                prompt_parts.append(f"Trading Signal:\n{signal_summary}")
            
            # Дополнительный контекст
            if request.additional_context:
                context_summary = self._summarize_additional_context(request.additional_context)
                if context_summary:
                    prompt_parts.append(f"Additional Context:\n{context_summary}")
            
            # Специфичные инструкции по типу анализа
            type_instructions = self._get_type_specific_instructions(request.analysis_type)
            prompt_parts.append(type_instructions)
            
            full_prompt = "\n\n".join(prompt_parts)
            
            # Обрезаем если слишком длинный
            if len(full_prompt) > MAX_PROMPT_LENGTH:
                full_prompt = full_prompt[:MAX_PROMPT_LENGTH] + "..."
            
            return full_prompt
            
        except Exception as e:
            self.logger.error(f"❌ Prompt generation failed: {e}")
            return None
    
    async def _summarize_market_data(self, market_data: List[CandleData]) -> str:
        """Создание сводки рыночных данных"""
        
        if not market_data:
            return "No market data available"
        
        try:
            # Берем последние 50 свечей для анализа
            recent_data = market_data[-50:] if len(market_data) > 50 else market_data
            
            # Основные метрики
            current_price = recent_data[-1].close_price
            open_price = recent_data[0].open_price
            high_price = max(candle.high_price for candle in recent_data)
            low_price = min(candle.low_price for candle in recent_data)
            
            # Изменение цены
            price_change = (current_price - open_price) / open_price * 100
            
            # Объем
            total_volume = sum(candle.volume for candle in recent_data)
            avg_volume = total_volume / len(recent_data)
            
            # Волатильность (упрощенная)
            price_changes = []
            for i in range(1, len(recent_data)):
                change = (recent_data[i].close_price - recent_data[i-1].close_price) / recent_data[i-1].close_price
                price_changes.append(abs(change))
            
            volatility = sum(price_changes) / len(price_changes) * 100 if price_changes else 0
            
            summary = f"""Current Price: ${current_price:.4f}
Price Change: {price_change:+.2f}%
Price Range: ${low_price:.4f} - ${high_price:.4f}
Average Volume: {avg_volume:.2f}
Recent Volatility: {volatility:.2f}%
Data Points: {len(recent_data)} candles"""
            
            return summary
            
        except Exception as e:
            self.logger.error(f"❌ Market data summarization failed: {e}")
            return "Error summarizing market data"
    
    def _summarize_signal_data(self, signal: StrategyResult) -> str:
        """Создание сводки торгового сигнала"""
        
        try:
            summary = f"""Signal Type: {signal.signal_type.value}
Confidence: {signal.confidence:.2f}
Strength: {signal.strength.value}"""
            
            if signal.entry_price:
                summary += f"\nEntry Price: ${signal.entry_price:.4f}"
            
            if signal.stop_loss:
                summary += f"\nStop Loss: ${signal.stop_loss:.4f}"
            
            if signal.take_profit:
                summary += f"\nTake Profit: ${signal.take_profit:.4f}"
            
            if signal.reasoning:
                summary += f"\nReasoning: {signal.reasoning}"
            
            if signal.indicators_data:
                # Добавляем ключевые индикаторы
                indicators = []
                for key, value in signal.indicators_data.items():
                    if isinstance(value, (int, float)):
                        indicators.append(f"{key}: {value:.4f}")
                
                if indicators:
                    summary += f"\nKey Indicators: {', '.join(indicators[:5])}"  # Первые 5
            
            return summary
            
        except Exception as e:
            self.logger.error(f"❌ Signal data summarization failed: {e}")
            return "Error summarizing signal data"
    
    def _summarize_additional_context(self, context: Dict[str, Any]) -> str:
        """Создание сводки дополнительного контекста"""
        
        try:
            summary_parts = []
            
            if 'current_price' in context:
                summary_parts.append(f"Current Price: ${context['current_price']:.4f}")
            
            if 'portfolio_balance' in context:
                summary_parts.append(f"Portfolio Balance: ${context['portfolio_balance']:.2f}")
            
            if 'open_positions' in context:
                summary_parts.append(f"Open Positions: {context['open_positions']}")
            
            if 'market_trend' in context:
                summary_parts.append(f"Market Trend: {context['market_trend']}")
            
            if 'news' in context:
                news_count = len(context['news']) if isinstance(context['news'], list) else 1
                summary_parts.append(f"Recent News Items: {news_count}")
            
            if 'social' in context:
                social_count = len(context['social']) if isinstance(context['social'], list) else 1
                summary_parts.append(f"Social Mentions: {social_count}")
            
            return "\n".join(summary_parts) if summary_parts else ""
            
        except Exception as e:
            self.logger.error(f"❌ Context summarization failed: {e}")
            return ""
    
    def _get_type_specific_instructions(self, analysis_type: AnalysisType) -> str:
        """Получение специфичных инструкций по типу анализа"""
        
        instructions = {
            AnalysisType.TECHNICAL: """
Provide technical analysis focusing on:
1. Trend direction and strength
2. Key support/resistance levels  
3. Entry and exit points
4. Risk management suggestions
5. Price targets

Include confidence level based on technical pattern clarity.
""",
            AnalysisType.STRATEGY_VALIDATION: """
Evaluate the trading signal considering:
1. Signal quality and reliability
2. Market conditions suitability
3. Risk-reward ratio
4. Timing appropriateness
5. Alternative scenarios

Rate confidence based on signal strength and market context.
""",
            AnalysisType.RISK_ASSESSMENT: """
Focus on risk factors:
1. Position size recommendations
2. Stop-loss placement strategy
3. Potential loss scenarios
4. Correlation risks
5. Market volatility impact

Confidence should reflect certainty of risk assessment.
""",
            AnalysisType.SENTIMENT: """
Analyze market sentiment through:
1. News sentiment impact
2. Social media trends
3. Fear/greed indicators
4. Institutional activity
5. Contrarian opportunities

Base confidence on sentiment data quality and clarity.
""",
            AnalysisType.MARKET_OVERVIEW: """
Provide market overview covering:
1. Overall market direction
2. Key support/resistance for the market
3. Sector rotation trends
4. Macro factors influence
5. Short/medium term outlook

Confidence based on market signal consistency.
"""
        }
        
        return instructions.get(analysis_type, "Provide thorough analysis with clear reasoning.")
    
    def _get_recommendation_prompt(self) -> str:
        """Специальный промпт для торговых рекомендаций"""
        
        return """Based on the provided market data and context, provide specific trading recommendations including:

1. Suggested action (BUY/SELL/HOLD)
2. Entry price range
3. Stop-loss level
4. Take-profit targets
5. Position sizing recommendation
6. Time horizon
7. Key risks to monitor

Format your response as JSON with the structure provided in the system prompt.
Focus on actionable advice with clear risk management."""
    
    # ========================================================================
    # CACHING AND RATE LIMITING
    # ========================================================================
    
    def _generate_cache_key(self, request: AIAnalysisRequest) -> str:
        """Генерация ключа кеша для запроса"""
        
        # Создаем хеш на основе ключевых параметров
        key_data = {
            'analysis_type': request.analysis_type.value,
            'symbol': request.symbol,
            'timeframe': request.timeframe,
            'model': self.model
        }
        
        # Добавляем хеш рыночных данных если есть
        if request.market_data:
            # Используем последние 10 свечей для хеширования
            last_candles = request.market_data[-10:]
            market_hash = hashlib.md5(
                str([(c.open_time, c.close_price) for c in last_candles]).encode()
            ).hexdigest()[:8]
            key_data['market_hash'] = market_hash
        
        # Добавляем хеш сигнала если есть
        if request.signal_data:
            signal_hash = hashlib.md5(
                f"{request.signal_data.signal_type}{request.signal_data.confidence}".encode()
            ).hexdigest()[:8]
            key_data['signal_hash'] = signal_hash
        
        # Создаем финальный ключ
        key_string = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_string.encode()).hexdigest()[:16]
    
    def _get_from_cache(self, cache_key: str) -> Optional[AIAnalysisResponse]:
        """Получение результата из кеша"""
        
        if cache_key not in self._analysis_cache:
            return None
        
        # Проверяем TTL
        cache_time = self._cache_timestamps.get(cache_key, 0)
        current_time = get_current_timestamp()
        
        if (current_time - cache_time) > (CACHE_TTL_SECONDS * 1000):
            # Кеш устарел
            del self._analysis_cache[cache_key]
            del self._cache_timestamps[cache_key]
            return None
        
        return self._analysis_cache[cache_key]
    
    def _save_to_cache(self, cache_key: str, response: AIAnalysisResponse) -> None:
        """Сохранение результата в кеш"""
        
        self._analysis_cache[cache_key] = response
        self._cache_timestamps[cache_key] = get_current_timestamp()
        
        # Очищаем старые записи если кеш слишком большой
        if len(self._analysis_cache) > 100:
            self._cleanup_cache()
    
    def _cleanup_cache(self) -> None:
        """Очистка устаревших записей кеша"""
        
        current_time = get_current_timestamp()
        keys_to_remove = []
        
        for key, timestamp in self._cache_timestamps.items():
            if (current_time - timestamp) > (CACHE_TTL_SECONDS * 1000):
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            if key in self._analysis_cache:
                del self._analysis_cache[key]
            if key in self._cache_timestamps:
                del self._cache_timestamps[key]
        
        if keys_to_remove:
            self.logger.debug(f"🧹 Cleaned up {len(keys_to_remove)} cache entries")
    
    async def _check_rate_limits(self) -> bool:
        """Проверка лимитов запросов"""
        
        current_time = time.time()
        current_date = datetime.now(timezone.utc).date()
        
        # Сброс дневного счетчика
        if current_date != self._last_reset_date:
            self._daily_requests = 0
            self._last_reset_date = current_date
        
        # Проверка дневного лимита
        if self._daily_requests >= REQUESTS_PER_DAY:
            self.logger.warning(f"⚠️ Daily request limit exceeded: {self._daily_requests}")
            return False
        
        # Очистка старых запросов (старше 1 минуты)
        self._request_times = [t for t in self._request_times if current_time - t < 60]
        
        # Проверка минутного лимита
        if len(self._request_times) >= REQUESTS_PER_MINUTE:
            self.logger.warning(f"⚠️ Per-minute request limit exceeded: {len(self._request_times)}")
            return False
        
        # Добавляем текущий запрос
        self._request_times.append(current_time)
        self._daily_requests += 1
        
        return True
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def _determine_confidence_level(self, confidence: float) -> AnalysisConfidence:
        """Определение уровня уверенности"""
        
        if confidence >= 0.9:
            return AnalysisConfidence.VERY_HIGH
        elif confidence >= 0.7:
            return AnalysisConfidence.HIGH
        elif confidence >= 0.5:
            return AnalysisConfidence.MODERATE
        elif confidence >= 0.3:
            return AnalysisConfidence.LOW
        else:
            return AnalysisConfidence.VERY_LOW
    
    async def _save_analysis_to_db(
        self, 
        request: AIAnalysisRequest, 
        response: AIAnalysisResponse
    ) -> None:
        """Сохранение анализа в базу данных"""
        
        try:
            # Можно расширить для сохранения в специальную таблицу AI анализов
            # Пока логируем для отладки
            self.logger.debug(f"💾 AI analysis completed: {request.analysis_type.value} for {request.symbol}")
            
        except Exception as e:
            self.logger.error(f"❌ Failed to save analysis to DB: {e}")
    
    def clear_cache(self) -> None:
        """Очистка всего кеша"""
        self._analysis_cache.clear()
        self._cache_timestamps.clear()
        self.logger.info("🧹 AI analysis cache cleared")
    
    def get_stats(self) -> Dict[str, Any]:
        """Получение статистики сервиса"""
        return {
            **self.stats,
            'cache_size': len(self._analysis_cache),
            'daily_requests': self._daily_requests,
            'requests_this_minute': len([t for t in self._request_times if time.time() - t < 60]),
            'api_key_configured': bool(self.api_key)
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья сервиса"""
        
        status = "healthy"
        issues = []
        
        if not self.api_key:
            status = "unhealthy"
            issues.append("API key not configured")
        
        if self._daily_requests >= REQUESTS_PER_DAY:
            status = "degraded"
            issues.append("Daily rate limit reached")
        
        return {
            'status': status,
            'issues': issues,
            'api_configured': bool(self.api_key),
            'daily_requests': self._daily_requests,
            'cache_size': len(self._analysis_cache),
            'session_active': self._session is not None
        }


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import asyncio
    from strategies.base_strategy import create_strategy_result
    
    async def test_openai_service():
        """Тестирование OpenAI Service"""
        print("🧪 Testing OpenAI Service")
        print("=" * 50)
        
        try:
            async with OpenAIService() as service:
                
                # Health check
                health = await service.health_check()
                print(f"🏥 Health: {health['status']}")
                
                if not health['api_configured']:
                    print("⚠️ API key not configured, skipping API tests")
                    return
                
                # Создаем тестовый сигнал
                test_signal = create_strategy_result(
                    SignalType.BUY,
                    confidence=0.85,
                    entry_price=50000.0,
                    reasoning="Strong upward momentum with volume confirmation"
                )
                
                # Тест анализа сигнала
                print("\n📝 Testing signal analysis...")
                analysis = await service.analyze_signal(
                    signal=test_signal,
                    symbol="BTCUSDT",
                    timeframe="5m"
                )
                
                if analysis:
                    print(f"✅ Analysis completed: confidence={analysis.confidence:.2f}")
                    print(f"Key points: {analysis.key_points[:2]}...")
                else:
                    print("❌ Analysis failed")
                
                # Статистика
                stats = service.get_stats()
                print(f"📊 Stats: {stats}")
                
                print("\n✅ OpenAI Service test completed!")
                
        except Exception as e:
            print(f"❌ Test failed: {e}")
    
    # Запуск теста
    asyncio.run(test_openai_service())
