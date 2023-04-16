import ccxt
import json
import logging
import requests
import API_Config 
from time import sleep

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%d-%m-%Y %H:%M:%S',
    filename='DipBot.log'
    )

# Define Constant Variables
MAX_RETRIES = 5
RETRY_SLEEP = 60

# Variables section required for initializing
''' Variables for retrieving API & Telegram Keys'''
API_KEY = API_Config.exchange_config.get('API_KEY')
API_SECRET = API_Config.exchange_config.get('API_SECRET')
TELEGRAM_TOKEN = API_Config.telegram_config.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = API_Config.telegram_config.get('TELEGRAM_CHAT_ID')

EXCHANGE = ccxt.woo({
    'enableRateLimiter': True,
    'apiKey': API_KEY,
    'secret': API_SECRET,
    })

def send_telegram_message(message):
    """
    Send a message to a Telegram chat.

    Args:
        message (str): The message to be sent.

    Returns:
        dict: The response from the Telegram API in JSON format.
    """
    # Check if the message contains a JSON code block, and format it for better readability
    if '<pre language="json"' in message:
        message = message.replace('<pre language="json"', f'{"-"*24}\n| Returned Information |\n{"-"*24}\n<pre language="json"')
    # Construct the URL for the Telegram API with the message, chat ID, and parse mode
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id=-{TELEGRAM_CHAT_ID}&text={message}&parse_mode=html"
    # Print the constructed URL for debugging or logging purposes
    print(url)
    # Send the HTTP GET request to the Telegram API and parse the response as JSON
    return requests.get(url).json()

def calculate_perc_difference(original: float, new: float):
    """
    Calculate the percentage difference between an original value and a new value.

    Args:
        original (float): The original value.
        new (float): The new value.

    Returns:
        float: The calculated percentage difference.
    """
    # Calculate the percentage difference using the provided formula
    perc_diff = round(((new - original) / original) * 100, 2)
    # Return the calculated percentage difference
    return perc_diff

def get_max_position_size(symbol, execute_price: float = None):
    """
    Get the maximum position size for a given symbol based on leverage information.

    Args:
        symbol (str): The symbol for which to fetch leverage information.
        execute_price (float, optional): The execution price for the symbol. Defaults to None.

    Returns:
        float: The calculated maximum position size.
    """
    try:
        # Fetch leverage information for the given symbol from the exchange
        acc_info = EXCHANGE.fetch_leverage(symbol)['info']
    except Exception as e:
        # Log and notify if fetching leverage information fails
        logging.error(f'Failed to fetch leverage information for symbol: {symbol}. Error: {e}')
        send_telegram_message(f'CDB-[Perp Fetch Leverage]: Failed to fetch leverage information for symbol: {symbol}. Error: {e}')
        return 
    else:
        # Extract total collateral and free collateral from the fetched leverage information
        total_collateral = float(acc_info['data']['totalCollateral'])
        free_collateral = float(acc_info['data']['freeCollateral'])
        # Set leverage to 5x (instead of using the account leverage)
        leverage = 5
        # Check if free collateral is less than 25% of total collateral, if so, return 0 as max position size
        # This will prevent over-leveraging and get liquidated
        if free_collateral < (total_collateral * 0.25):
            return 0
        # Calculate max position size as 75% of the product of free collateral and leverage
        # Another prevention of over-leveraging by only returning 75% of what is the max position size
        max_position_size = ((free_collateral * leverage) * 0.75)
        # Return max position size divided by the execute price
        return max_position_size / execute_price

def log_send(message, type:str='info'):
    """
    Logs a message with the specified type and sends it to Telegram.

    Args:
        message (str): The message to be logged and sent.
        type (str, optional): The type of the message (info, warning, or error). Default is 'info'.

    Returns:
        None
    """
    if type == 'info':
        logging.info(message)
    elif type == 'warning':
        logging.warning(message)
    elif type == 'error':
        logging.error(message)
    send_telegram_message(message)
    return

def execute_spot(symbol, config):
    """
    Execute a spot trade on a cryptocurrency exchange.

    Args:
        symbol (str): The trading symbol of the cryptocurrency.
        config (dict): Configuration parameters for the spot trade.

    Returns:
        str: The status of the trade execution (Success, Success - Minimum Size, Failed).
    """
    symbol = symbol + '/USDT'
    # Define constants
    EXECUTION_SOURCE_MAP = {
        "open": 1,
        "high": 2,
        "low": 3,
        "close": 4
    }

    # Retrieve configuration parameters
    spot_info = config['spot']
    enabled_spot = spot_info['enabled']
    timeframe = spot_info['candle_timeframe']
    purchase_percent = spot_info['spot_purchase_percent']
    candle_threshold = spot_info['spot_candle_threshold']
    execution_source = spot_info['execution_source']

    # Fetch balance and OHLC data
    usdt_balance = float(EXCHANGE.fetch_free_balance()['USDT'])
    # Fetch the last confirmed candle
    ohlc = EXCHANGE.fetch_ohlcv(symbol, timeframe)[-2]

    # Calculate percentage difference between high and low
    hl_difference = calculate_perc_difference(ohlc[2], ohlc[3])

    # Retrieve the corresponding index from the dictionary
    execution_index = EXECUTION_SOURCE_MAP.get(execution_source.lower())

    # Check if the retrieved index is valid
    if execution_index is None:
        execution_point = None
        # Handle invalid values for 'execution_source' here
        return log_send("Invalid value for 'execution_source' in JSON file.", 'warning')
    else:
        # Assign variable based on the retrieved index
        execution_point = ohlc[execution_index]
        # Check if account have sufficient balance
        if usdt_balance >= (usdt_balance * (purchase_percent / 100)):
            size = (usdt_balance * (purchase_percent / 100)) / execution_point
        else:
            return log_send('CDB [USDT Balance]: Insufficient funds.', 'warning')

    if enabled_spot:
        # Check if HL difference is within threshold
        if hl_difference <= candle_threshold:
            # Calculate spot size based on purchase percentage and closing price
            spot_size = EXCHANGE.amount_to_precision(symbol, size)
            while True:
                try:
                    # Place limit order to buy
                    trade_response = EXCHANGE.create_limit_order(symbol, 'buy', spot_size, execution_point)
                    if trade_response['info']['success']:
                        return log_send(f'CDB [Spot Trade]: {spot_size}{symbol} executed at {execution_point}', 'info')
                except Exception as e:
                    for _ in range(1, MAX_RETRIES):
                        if 'greater than minimum amount' in str(e):
                            # If order size is below minimum amount, retry with minimum amount
                            minimum_amount = float(str(e).split('precision of')[1])
                            trade_response = EXCHANGE.create_limit_order(symbol, 'buy', minimum_amount, execution_point)
                            return log_send(f'CDB [Spot Trade]: [Minimum Size]{spot_size}{symbol} executed at {execution_point}', 'warning')
                        sleep(RETRY_SLEEP)
                    return log_send(f'CDB [Spot Trade]: Failed - {e}')
                        
def execute_perp(symbol, config):
    """
    Execute a perpetual trade on a cryptocurrency exchange.

    Args:
        symbol (str): The trading symbol of the cryptocurrency.
        config (dict): Configuration parameters for the perpetual trade.

    Returns:
        str: The status of the trade execution (Success, Success - Minimum Size, Failed).
    """
    symbol = symbol + '/USDT:USDT'

    # Define constants
    EXECUTION_SOURCE_MAP = {
        "open": 1,
        "high": 2,
        "low": 3,
        "close": 4
    }

    # Retrieve configuration parameters
    perp_info = config['perp']
    enabled_perp = perp_info['enabled']
    timeframe = perp_info['candle_timeframe']
    purchase_percent = perp_info['perp_purchase_percent']
    candle_threshold = perp_info['perp_candle_threshold']
    execution_source = perp_info['execution_source']

    # Fetch the last confirmed candle
    ohlc = EXCHANGE.fetch_ohlcv(symbol, timeframe)[-2]

    # Calculate percentage difference between high and low
    hl_difference = calculate_perc_difference(ohlc[2], ohlc[3])

    # Retrieve the corresponding index from the dictionary
    execution_index = EXECUTION_SOURCE_MAP.get(execution_source.lower())

    # Check if the retrieved index is valid
    if execution_index is None:
        execution_point = None
        # Handle invalid values for 'execution_source' here
        return log_send("Invalid value for 'execution_source' in JSON file.", 'warning')
    else:
        # Assign variable based on the retrieved index
        execution_point = ohlc[execution_index]
        position_size = get_max_position_size(symbol, execution_point) * (purchase_percent / 100)

    if enabled_perp:
        if hl_difference <= candle_threshold:
            if position_size is None:  # get Max Position will return 0 if insufficient collateral
                return log_send('CDB [Perp-Collateral]: Collateral insufficient (Dangerous to execute any more trades).')

            while True:
                try:
                    # Place limit order to buy
                    trade_response = EXCHANGE.create_limit_order(symbol, 'buy', position_size, execution_point)
                    if trade_response['info']['success']:
                        return log_send(f'CDB [Perp Trade]: {position_size}{symbol} executed at {execution_point}', 'info')
                except Exception as e:
                    for _ in range(1, MAX_RETRIES):
                        if 'greater than minimum amount' in str(e):
                            # If order size is below minimum amount, retry with minimum amount
                            minimum_amount = float(str(e).split('precision of')[1])
                            trade_response = EXCHANGE.create_limit_order(symbol, 'buy', minimum_amount, execution_point)
                            return log_send(f'CDB [Perp Trade]: [Minimum Size]{position_size}{symbol} executed at {execution_point}', 'warning')
                        sleep(RETRY_SLEEP)
                    return log_send(f'CDB [Perp Trade]: Failed - {e}')

calculate_perc_difference()

def main():
    # Load JSON data from file
    # This ensure that there is no need to restart the service after changes is made to json file.
    with open('Bot_Config.json', 'r') as file:
        Bot_Config = json.load(file)

    for symbol, config in Bot_Config.get('symbols').items():
        execute_spot(symbol, config)
        sleep(1)
        execute_perp(symbol, config)
        
if __name__ == '__main__':
    while True:
        main()
        print('Checked.')
        sleep(3600)