import logging
import os
from typing import TYPE_CHECKING

import shioaji as sj
from shioaji.contracts import Contract
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


def get_current_position(api: sj.Shioaji, contract: Contract):
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


def check_order_status(api: sj.Shioaji, trade) -> dict:
    """
    Check the actual fill status of an order by calling update_status.
    
    According to Shioaji source code:
    - update_status() updates the trade object in-place (doesn't return anything)
    - OrderStatus has: status, deal_quantity, cancel_quantity, deals, order_quantity
    - Status enum: PendingSubmit, PreSubmitted, Submitted, PartFilled, Filled, Cancelled, Failed, Inactive
    
    Ref: https://sinotrade.github.io/zh/tutor/order/FutureOption/#_2
    
    Returns a dict with:
        - status: str (PendingSubmit, Submitted, Filled, PartFilled, Cancelled, Failed, Inactive)
        - order_quantity: int
        - deal_quantity: int (filled quantity from OrderStatus)
        - cancel_quantity: int
        - deals: list of deal info (price, quantity, timestamp)
        - fill_avg_price: float (average fill price calculated from deals)
    """
    logger.debug(f"Checking order status for trade: {trade.order.id if trade else 'None'}")
    
    if trade is None:
        return {"status": "no_trade", "error": "No trade object provided"}
    
    try:
        # update_status() updates trade object in-place, passing trade= for specific trade update
        api.update_status(trade=trade)
        
        # Extract status info from updated trade object
        status_obj = trade.status
        order_obj = trade.order
        
        # Get deals list for calculating average price
        deals = status_obj.deals if status_obj.deals else []
        
        # Use deal_quantity from OrderStatus (this is the official filled quantity)
        deal_quantity = status_obj.deal_quantity if hasattr(status_obj, 'deal_quantity') else 0
        
        # Calculate average fill price from deals
        total_value = sum(d.price * d.quantity for d in deals) if deals else 0
        total_qty = sum(d.quantity for d in deals) if deals else 0
        fill_avg_price = total_value / total_qty if total_qty > 0 else 0.0
        
        # Get status value - Status is an Enum
        status_value = status_obj.status.value if hasattr(status_obj.status, 'value') else str(status_obj.status)
        
        result = {
            "status": status_value,
            "status_code": getattr(status_obj, 'status_code', ''),
            "msg": getattr(status_obj, 'msg', ''),
            "order_id": getattr(order_obj, 'id', ''),
            "seqno": getattr(order_obj, 'seqno', ''),
            "ordno": getattr(order_obj, 'ordno', ''),
            "order_quantity": getattr(status_obj, 'order_quantity', 0) or order_obj.quantity,
            "deal_quantity": deal_quantity,
            "cancel_quantity": getattr(status_obj, 'cancel_quantity', 0),
            "fill_avg_price": fill_avg_price,
            "deals": [
                {
                    "seq": getattr(d, 'seq', ''),
                    "price": d.price,
                    "quantity": d.quantity,
                    "ts": getattr(d, 'ts', 0),
                }
                for d in deals
            ],
        }
        
        logger.debug(f"Order status result: {result}")
        return result
        
    except Exception as e:
        logger.error(f"Error checking order status: {e}")
        return {"status": "error", "error": str(e)}
