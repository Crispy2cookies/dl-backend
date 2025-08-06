import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import psycopg2
from psycopg2.extras import RealDictCursor
from langchain_core.tools import tool
import json
import logging
from decimal import Decimal

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class JesseDatabase:
    def __init__(self):
        """Initialize Jesse database connection with improved configuration"""
        self.db_config = {
            'host': os.getenv('JESSE_DB_HOST', 'localhost'),
            'port': int(os.getenv('JESSE_DB_PORT', 5432)),
            'database': os.getenv('JESSE_DB_NAME', 'jesse_db'),
            'user': os.getenv('JESSE_DB_USER', 'jesse_user'),
            'password': os.getenv('JESSE_DB_PASSWORD', 'password')
        }
        
        # Cache for connection testing
        self._connection_tested = False
        self._connection_valid = False
        
    def get_connection(self):
        """Get database connection with better error handling"""
        try:
            conn = psycopg2.connect(**self.db_config)
            conn.set_session(autocommit=True)
            return conn
        except psycopg2.OperationalError as e:
            logger.error(f"Database connection error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected database error: {e}")
            return None
    
    def test_connection(self) -> bool:
        """Test database connection with caching"""
        if self._connection_tested:
            return self._connection_valid
            
        conn = self.get_connection()
        if not conn:
            self._connection_tested = True
            self._connection_valid = False
            return False
        
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';")
                tables = [row[0] for row in cursor.fetchall()]
                
                if 'candle' not in tables:
                    logger.warning("Candle table not found in database")
                    self._connection_valid = False
                else:
                    logger.info(f"Database connection successful. Tables: {tables}")
                    self._connection_valid = True
                    
        except Exception as e:
            logger.error(f"Error testing connection: {e}")
            self._connection_valid = False
        finally:
            conn.close()
            
        self._connection_tested = True
        return self._connection_valid
    
    def get_available_symbols(self) -> List[Tuple[str, str]]:
        """Get all available trading pairs from the database"""
        conn = self.get_connection()
        if not conn:
            return []
        
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT DISTINCT symbol, exchange 
                    FROM candle 
                    ORDER BY symbol, exchange
                """)
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Error fetching available symbols: {e}")
            return []
        finally:
            conn.close()
    
    def get_data_range(self, symbol: str, exchange: str = 'Binance Perpetual Futures') -> Dict:
        """Get the date range of available data for a symbol"""
        conn = self.get_connection()
        if not conn:
            return {}
        
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        MIN(to_timestamp(timestamp/1000)) as earliest_date,
                        MAX(to_timestamp(timestamp/1000)) as latest_date,
                        COUNT(*) as total_candles
                    FROM candle 
                    WHERE symbol = %s AND exchange = %s
                """, (symbol, exchange))
                
                result = cursor.fetchone()
                if result and result[0]:
                    return {
                        'earliest_date': result[0],
                        'latest_date': result[1], 
                        'total_candles': result[2]
                    }
                return {}
        except Exception as e:
            logger.error(f"Error fetching data range: {e}")
            return {}
        finally:
            conn.close()
    
    def get_historical_prices(self, symbol: str, days_back: int = 365, 
                            exchange: str = 'Binance Perpetual Futures',
                            timeframe: str = '1m') -> List[Dict]:
        """
        Get historical prices with improved error handling and flexibility
        """
        conn = self.get_connection()
        if not conn:
            return []
        
        try:
            # Normalize symbol format
            jesse_symbol = symbol.replace('/', '-').upper()
            
            # Calculate date range
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days_back)
            
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Check if we have data for this symbol
                cursor.execute("""
                    SELECT COUNT(*) as count FROM candle 
                    WHERE symbol = %s AND exchange = %s
                """, (jesse_symbol, exchange))
                
                count_result = cursor.fetchone()
                if not count_result['count']:
                    logger.warning(f"No data found for {jesse_symbol} on {exchange}")
                    return []
                
                # Query for daily aggregated data
                query = """
                WITH daily_candles AS (
                    SELECT 
                        DATE(to_timestamp(timestamp/1000)) as trade_date,
                        timestamp,
                        open::float as open,
                        high::float as high,
                        low::float as low,
                        close::float as close,
                        volume::float as volume,
                        ROW_NUMBER() OVER (
                            PARTITION BY DATE(to_timestamp(timestamp/1000)) 
                            ORDER BY timestamp ASC
                        ) as rn_asc,
                        ROW_NUMBER() OVER (
                            PARTITION BY DATE(to_timestamp(timestamp/1000)) 
                            ORDER BY timestamp DESC
                        ) as rn_desc
                    FROM candle 
                    WHERE symbol = %s 
                    AND exchange = %s
                    AND timeframe = %s
                    AND to_timestamp(timestamp/1000) >= %s
                    AND to_timestamp(timestamp/1000) <= %s
                )
                SELECT 
                    trade_date,
                    MAX(CASE WHEN rn_asc = 1 THEN open END) as open,
                    MAX(high) as high,
                    MIN(low) as low,
                    MAX(CASE WHEN rn_desc = 1 THEN close END) as close,
                    SUM(volume) as volume
                FROM daily_candles
                GROUP BY trade_date
                ORDER BY trade_date ASC
                """
                
                cursor.execute(query, (
                    jesse_symbol, 
                    exchange, 
                    timeframe,
                    start_date, 
                    end_date
                ))
                
                results = cursor.fetchall()
                logger.info(f"Retrieved {len(results)} daily candles for {symbol}")
                
                # Convert to structured format
                candles = []
                for row in results:
                    if row['open'] and row['close']:  # Ensure we have valid data
                        candles.append({
                            'date': row['trade_date'].strftime('%Y-%m-%d'),
                            'open': float(row['open']),
                            'high': float(row['high']),
                            'low': float(row['low']),
                            'close': float(row['close']),
                            'volume': float(row['volume'] or 0)
                        })
                
                return candles
                
        except Exception as e:
            logger.error(f"Error fetching historical data for {symbol}: {e}")
            return []
        finally:
            conn.close()
    
    def calculate_technical_indicators(self, candles: List[Dict]) -> Dict:
        """Calculate basic technical indicators"""
        if len(candles) < 20:
            return {}
        
        closes = [c['close'] for c in candles]
        
        # Simple Moving Averages
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        sma_200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        
        # Calculate volatility (standard deviation of returns)
        returns = []
        for i in range(1, len(closes)):
            returns.append((closes[i] - closes[i-1]) / closes[i-1])
        
        volatility = None
        if len(returns) > 1:
            mean_return = sum(returns) / len(returns)
            variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
            volatility = variance ** 0.5
        
        return {
            'sma_20': round(sma_20, 2),
            'sma_50': round(sma_50, 2) if sma_50 else None,
            'sma_200': round(sma_200, 2) if sma_200 else None,
            'volatility_20d': round(volatility * 100, 2) if volatility else None
        }
    
    def calculate_price_changes(self, symbol: str, 
                              exchange: str = 'Binance Perpetual Futures') -> Dict:
        """Enhanced price change calculation with technical indicators"""
        candles = self.get_historical_prices(symbol, days_back=400, exchange=exchange)
        
        if not candles:
            # Try to get available symbols and suggest alternatives
            available = self.get_available_symbols()
            available_symbols = [f"{s[0]} on {s[1]}" for s in available[:5]]
            
            return {
                "error": f"No historical data found for {symbol} on {exchange}",
                "available_symbols": available_symbols,
                "suggestion": "Check available symbols and ensure data is imported in Jesse"
            }
        
        current_price = candles[-1]['close']
        
        def get_price_n_days_ago(days: int) -> Optional[float]:
            target_index = len(candles) - days - 1
            return candles[target_index]['close'] if target_index >= 0 else None
        
        def calculate_change(old_price: float, new_price: float) -> Dict:
            if old_price == 0:
                return {"change": 0, "percentage": 0, "direction": "NEUTRAL"}
            
            change = new_price - old_price
            percentage = (change / old_price) * 100
            
            return {
                "change": round(change, 4),
                "percentage": round(percentage, 2),
                "direction": "UP" if change > 0 else "DOWN" if change < 0 else "NEUTRAL",
                "old_price": round(old_price, 4),
                "new_price": round(new_price, 4)
            }
        
        # Time periods for analysis
        periods = {
            "1_day": 1,
            "3_days": 3,
            "7_days": 7,
            "14_days": 14,
            "30_days": 30,
            "90_days": 90,
            "200_days": 200,
            "1_year": 365
        }
        
        changes = {}
        for period_name, days in periods.items():
            old_price = get_price_n_days_ago(days)
            if old_price:
                changes[period_name] = calculate_change(old_price, current_price)
            else:
                changes[period_name] = {"error": f"Insufficient data for {period_name}"}
        
        # Calculate technical indicators
        technical_indicators = self.calculate_technical_indicators(candles)
        
        # Get data range info
        data_range = self.get_data_range(symbol.replace('/', '-').upper(), exchange)
        
        return {
            "symbol": symbol,
            "exchange": exchange,
            "current_price": round(current_price, 4),
            "changes": changes,
            "technical_indicators": technical_indicators,
            "data_info": {
                "data_points": len(candles),
                "date_range": data_range,
                "last_updated": candles[-1]['date'] if candles else None
            }
        }

# Initialize database instance
jesse_db = JesseDatabase()

@tool
def get_crypto_historical_analysis(symbol: str, exchange: str = "Binance Perpetual Futures") -> str:
    """
    Get comprehensive historical price analysis for a cryptocurrency using Jesse.ai database.
    
    Args:
        symbol: The cryptocurrency symbol (e.g., 'BTC/USDT', 'ETH/USDT')
        exchange: The exchange name (default: 'Binance Perpetual Futures')
    
    Returns:
        Formatted string with price changes and technical analysis
    """
    try:
        if not jesse_db.test_connection():
            return "❌ Cannot connect to Jesse database. Please check your database configuration."
        
        analysis = jesse_db.calculate_price_changes(symbol, exchange)
        
        if "error" in analysis:
            response = f"❌ Error analyzing {symbol}: {analysis['error']}\n"
            if "available_symbols" in analysis:
                response += f"💡 Available symbols: {', '.join(analysis['available_symbols'][:3])}"
            return response
        
        # Format the response
        symbol_name = symbol.split('/')[0] if '/' in symbol else symbol.split('-')[0]
        crypto_names = {
            'BTC': 'Bitcoin (BTC)',
            'ETH': 'Ethereum (ETH)', 
            'ETC': 'Ethereum Classic (ETC)',
            'ADA': 'Cardano (ADA)',
            'DOT': 'Polkadot (DOT)',
            'LINK': 'Chainlink (LINK)'
        }
        full_name = crypto_names.get(symbol_name, f"{symbol_name}")
        
        response = f"📊 **Price Analysis for {full_name}**\n"
        response += f"💱 Exchange: {analysis['exchange']}\n"
        response += f"💰 Current Price: ${analysis['current_price']:,.4f}\n\n"
        
        # Price changes
        response += "📈 **Price Changes:**\n"
        changes = analysis.get('changes', {})
        
        period_labels = {
            "1_day": "24 hours",
            "3_days": "3 days",
            "7_days": "7 days",
            "14_days": "14 days", 
            "30_days": "30 days",
            "90_days": "90 days",
            "200_days": "200 days",
            "1_year": "1 year"
        }
        
        for period_key, period_label in period_labels.items():
            if period_key in changes and "error" not in changes[period_key]:
                change_data = changes[period_key]
                direction = change_data['direction']
                percentage = change_data['percentage']
                
                emoji = "🟢" if direction == "UP" else "🔴" if direction == "DOWN" else "⚪"
                response += f"  {emoji} {period_label}: **{percentage:+.2f}%**\n"
        
        # Technical indicators
        if 'technical_indicators' in analysis:
            indicators = analysis['technical_indicators']
            response += "\n🔧 **Technical Indicators:**\n"
            
            if indicators.get('sma_20'):
                response += f"  📊 SMA 20: ${indicators['sma_20']:,.2f}\n"
            if indicators.get('sma_50'):
                response += f"  📊 SMA 50: ${indicators['sma_50']:,.2f}\n"
            if indicators.get('sma_200'):
                response += f"  📊 SMA 200: ${indicators['sma_200']:,.2f}\n"
            if indicators.get('volatility_20d'):
                response += f"  📊 20-day Volatility: {indicators['volatility_20d']:.2f}%\n"
        
        # Data info
        data_info = analysis.get('data_info', {})
        response += f"\n📋 **Data Info:**\n"
        response += f"  📅 Analysis Period: {data_info.get('data_points', 0)} days\n"
        response += f"  🕒 Last Updated: {data_info.get('last_updated', 'Unknown')}\n"
        
        if data_info.get('date_range'):
            dr = data_info['date_range']
            response += f"  📊 Total Records: {dr.get('total_candles', 'N/A'):,}\n"
        
        return response
        
    except Exception as e:
        logger.error(f"Error in crypto analysis: {e}")
        return f"❌ Unexpected error analyzing {symbol}: {str(e)}"

@tool
def get_available_crypto_symbols() -> str:
    """
    Get list of all available cryptocurrency symbols in the Jesse database.
    
    Returns:
        Formatted string listing all available trading pairs
    """
    try:
        if not jesse_db.test_connection():
            return "❌ Cannot connect to Jesse database."
        
        symbols = jesse_db.get_available_symbols()
        
        if not symbols:
            return "❌ No trading pairs found in database."
        
        response = "📊 **Available Trading Pairs in Jesse Database:**\n\n"
        
        # Group by exchange
        by_exchange = {}
        for symbol, exchange in symbols:
            if exchange not in by_exchange:
                by_exchange[exchange] = []
            by_exchange[exchange].append(symbol)
        
        for exchange, symbol_list in by_exchange.items():
            response += f"🏪 **{exchange}:**\n"
            for symbol in sorted(symbol_list):
                response += f"  • {symbol}\n"
            response += "\n"
        
        response += f"📈 Total: {len(symbols)} trading pairs across {len(by_exchange)} exchanges"
        
        return response
        
    except Exception as e:
        return f"❌ Error fetching available symbols: {str(e)}"

@tool
def get_crypto_raw_historical_data(symbol: str, days: int = 30, 
                                 exchange: str = "Binance Perpetual Futures") -> str:
    """
    Get raw historical OHLCV data for a cryptocurrency from Jesse.ai database.
    
    Args:
        symbol: The cryptocurrency symbol (e.g., 'BTC/USDT', 'ETC/USDT')
        days: Number of days of historical data to retrieve (default: 30)
        exchange: The exchange name (default: 'Binance Perpetual Futures')
        
    Returns:
        JSON string with historical OHLCV data
    """
    try:
        candles = jesse_db.get_historical_prices(symbol, days_back=days, exchange=exchange)
        
        if not candles:
            return json.dumps({
                "error": f"No historical data found for {symbol} on {exchange}",
                "symbol": symbol,
                "exchange": exchange,
                "days_requested": days
            }, indent=2)
        
        return json.dumps({
            "symbol": symbol,
            "exchange": exchange,
            "days_requested": days,
            "data_points": len(candles),
            "date_range": {
                "start": candles[0]['date'],
                "end": candles[-1]['date']
            },
            "current_price": candles[-1]['close'],
            "data": candles
        }, indent=2)
        
    except Exception as e:
        return json.dumps({
            "error": f"Error fetching raw data for {symbol}: {str(e)}"
        }, indent=2)

def main():
    """Main function for testing"""
    print("🔍 Testing Enhanced Jesse Database Integration...")
    
    # Test connection
    if not jesse_db.test_connection():
        print("❌ Database connection failed")
        return
    
    print("✅ Database connection successful!")
    
    # Test available symbols
    print("\n📊 Available symbols:")
    print(get_available_crypto_symbols())
    
    # Test analysis
    print("\n📈 Testing BTC analysis:")
    btc_analysis = get_crypto_historical_analysis("BTC/USDT")
    print(btc_analysis)

if __name__ == "__main__":
    main()