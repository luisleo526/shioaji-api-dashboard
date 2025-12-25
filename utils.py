import logging
import os

import shioaji as sj
from shioaji.error import (
    TokenError,
    SystemMaintenance,
    TimeoutError as SjTimeoutError,
    AccountNotSignError,
    AccountNotProvideError,
    TargetContractNotExistError,
)

logger = logging.getLogger(__name__)


class ShioajiError(Exception):
    """Base exception for Shioaji operations."""
    pass


class LoginError(ShioajiError):
    """Raised when login fails."""
    pass


class OrderError(ShioajiError):
    """Raised when order placement fails."""
    pass


def get_api_client(simulation: bool = True):
    logger.debug(f"Creating API client with simulation={simulation}")
    
    api_key = os.getenv("API_KEY")
    secret_key = os.getenv("SECRET_KEY")
    
    if not api_key or not secret_key:
        logger.error("API_KEY or SECRET_KEY environment variable not set")
        raise LoginError("API_KEY or SECRET_KEY environment variable not set")
    
    try:
        api = sj.Shioaji(simulation=simulation)
        api.login(api_key=api_key, secret_key=secret_key)
        logger.debug("API client logged in successfully")
        return api
    except TokenError as e:
        logger.error(f"Authentication failed: {e}")
        raise LoginError(f"Authentication failed: {e}") from e
    except SystemMaintenance as e:
        logger.error(f"System is under maintenance: {e}")
        raise LoginError(f"System is under maintenance: {e}") from e
    except SjTimeoutError as e:
        logger.error(f"Login timeout: {e}")
        raise LoginError(f"Login timeout: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error during login: {e}")
        raise LoginError(f"Unexpected error during login: {e}") from e


def get_valid_symbols(api: sj.Shioaji):
    return [
        contract.symbol
        for contract in api.Contracts.Futures.MXF
        if contract.symbol.startswith("MXF")
    ] + [
        contract.symbol
        for contract in api.Contracts.Futures.TXF
        if contract.symbol.startswith("TXF")
    ]


def get_valid_contract_codes(api: sj.Shioaji):
    return [
        contract.code
        for contract in api.Contracts.Futures.MXF
        if contract.code.startswith("MXF")
    ] + [
        contract.code
        for contract in api.Contracts.Futures.TXF
        if contract.code.startswith("TXF")
    ]


def get_contract_from_symbol(api: sj.Shioaji, symbol: str):
    for contract in api.Contracts.Futures.MXF:
        if contract.symbol == symbol:
            return contract
    for contract in api.Contracts.Futures.TXF:
        if contract.symbol == symbol:
            return contract
    raise ValueError(f"Contract {symbol} not found")


def get_contract_from_contract_code(api: sj.Shioaji, contract_code: str):
    for contract in api.Contracts.Futures.MXF:
        if contract.code == contract_code:
            return contract
    for contract in api.Contracts.Futures.TXF:
        if contract.code == contract_code:
            return contract
    raise ValueError(f"Contract {contract_code} not found")


def get_current_position(api: sj.Shioaji, contract: sj.Contract):
    logger.debug(f"Getting current position for contract: {contract.code}")
    for position in api.list_positions(api.futopt_account):
        if contract.code == position.code:
            if position.side == sj.constant.Action.Buy:
                logger.debug(f"Found long position: {position.quantity}")
                return position.quantity
            elif position.side == sj.constant.Action.Sell:
                logger.debug(f"Found short position: {-position.quantity}")
                return -position.quantity
            else:
                raise ValueError(f"Position {position.code} has invalid side")
    logger.debug("No position found")
    return None


def place_entry_order(
    api: sj.Shioaji, symbol: str, quantity: int, action: sj.constant.Action
):
    logger.debug(f"Placing entry order: symbol={symbol}, quantity={quantity}, action={action}")
    
    try:
        contract = get_contract_from_symbol(api, symbol)
    except ValueError as e:
        logger.error(f"Contract not found: {e}")
        raise OrderError(f"Contract not found: {e}") from e
    
    try:
        current_position = get_current_position(api, contract) or 0
        logger.debug(f"Current position: {current_position}")
    except (AccountNotSignError, AccountNotProvideError) as e:
        logger.error(f"Account error when getting position: {e}")
        raise OrderError(f"Account error: {e}") from e

    original_quantity = quantity
    if action == sj.constant.Action.Buy and current_position < 0:
        quantity = quantity - current_position
        logger.debug(f"Adjusting quantity for short reversal: {original_quantity} -> {quantity}")
    elif action == sj.constant.Action.Sell and current_position > 0:
        quantity = quantity + current_position
        logger.debug(f"Adjusting quantity for long reversal: {original_quantity} -> {quantity}")

    order = api.Order(
        action=action,
        price=0.0,
        quantity=quantity,
        price_type=sj.constant.FuturesPriceType.MKT,
        order_type=sj.constant.OrderType.IOC,
        octype=sj.constant.FuturesOCType.Auto,
        account=api.futopt_account,
    )

    try:
        logger.debug(f"Submitting order: action={action}, quantity={quantity}")
        result = api.place_order(contract, order)
        logger.debug(f"Order result: {result}")
        return result
    except TargetContractNotExistError as e:
        logger.error(f"Target contract not exist: {e}")
        raise OrderError(f"Target contract not exist: {e}") from e
    except SjTimeoutError as e:
        logger.error(f"Order timeout: {e}")
        raise OrderError(f"Order timeout: {e}") from e
    except (AccountNotSignError, AccountNotProvideError) as e:
        logger.error(f"Account error when placing order: {e}")
        raise OrderError(f"Account error: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error when placing order: {e}")
        raise OrderError(f"Unexpected error when placing order: {e}") from e


def place_exit_order(api: sj.Shioaji, symbol: str, position_direction: sj.constant.Action):
    logger.debug(f"Placing exit order: symbol={symbol}, position_direction={position_direction}")
    
    try:
        contract = get_contract_from_symbol(api, symbol)
    except ValueError as e:
        logger.error(f"Contract not found: {e}")
        raise OrderError(f"Contract not found: {e}") from e
    
    try:
        current_position = get_current_position(api, contract) or 0
        logger.debug(f"Current position: {current_position}")
    except (AccountNotSignError, AccountNotProvideError) as e:
        logger.error(f"Account error when getting position: {e}")
        raise OrderError(f"Account error: {e}") from e

    # close long
    if position_direction == sj.constant.Action.Buy and current_position > 0:
        logger.debug(f"Closing long position: selling {current_position}")
        order = api.Order(
            action=sj.constant.Action.Sell,
            price=0.0,
            quantity=current_position,
            price_type=sj.constant.FuturesPriceType.MKT,
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Auto,
            account=api.futopt_account,
        )
    # close short
    elif position_direction == sj.constant.Action.Sell and current_position < 0:
        logger.debug(f"Closing short position: buying {-current_position}")
        order = api.Order(
            action=sj.constant.Action.Buy,
            price=0.0,
            quantity=-current_position,
            price_type=sj.constant.FuturesPriceType.MKT,
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Auto,
            account=api.futopt_account,
        )
    else:
        logger.debug("No position to exit")
        return None

    try:
        result = api.place_order(contract, order)
        logger.debug(f"Order result: {result}")
        return result
    except TargetContractNotExistError as e:
        logger.error(f"Target contract not exist: {e}")
        raise OrderError(f"Target contract not exist: {e}") from e
    except SjTimeoutError as e:
        logger.error(f"Order timeout: {e}")
        raise OrderError(f"Order timeout: {e}") from e
    except (AccountNotSignError, AccountNotProvideError) as e:
        logger.error(f"Account error when placing order: {e}")
        raise OrderError(f"Account error: {e}") from e
    except Exception as e:
        logger.error(f"Unexpected error when placing order: {e}")
        raise OrderError(f"Unexpected error when placing order: {e}") from e
