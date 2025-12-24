import logging
import os

import shioaji as sj

logger = logging.getLogger(__name__)


def get_api_client(simulation: bool = True):
    logger.debug(f"Creating API client with simulation={simulation}")
    api = sj.Shioaji(simulation=simulation)
    api.login(
        api_key=os.getenv("API_KEY"),
        secret_key=os.getenv("SECRET_KEY"),
    )
    logger.debug("API client logged in successfully")
    return api


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
    contract = get_contract_from_symbol(api, symbol)
    current_position = get_current_position(api, contract) or 0
    logger.debug(f"Current position: {current_position}")

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

    logger.debug(f"Submitting order: action={action}, quantity={quantity}")
    result = api.place_order(contract, order)
    logger.debug(f"Order result: {result}")
    return result


def place_exit_order(api: sj.Shioaji, symbol: str, position_direction: sj.constant.Action):
    logger.debug(f"Placing exit order: symbol={symbol}, position_direction={position_direction}")
    contract = get_contract_from_symbol(api, symbol)
    current_position = get_current_position(api, contract) or 0
    logger.debug(f"Current position: {current_position}")

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
        result = api.place_order(contract, order)
        logger.debug(f"Order result: {result}")
        return result
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
        result = api.place_order(contract, order)
        logger.debug(f"Order result: {result}")
        return result
    logger.debug("No position to exit")
    return None
