import os

import shioaji as sj


def get_api_client(simulation: bool = True):
    api = sj.Shioaji(simulation=simulation)
    api.login(
        api_key=os.getenv("API_KEY"),
        secret_key=os.getenv("SECRET_KEY"),
    )

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
    for position in api.list_positions(api.futopt_account):
        if contract.code == position.code:
            if position.side == sj.constant.Action.Buy:
                return position.quantity
            elif position.side == sj.constant.Action.Sell:
                return -position.quantity
            else:
                raise ValueError(f"Position {position.code} has invalid side")
    return None


def place_entry_order(
    api: sj.Shioaji, symbol: str, quantity: int, action: sj.constant.Action
):
    contract = get_contract_from_symbol(api, symbol)
    current_position = get_current_position(api, contract)

    if action == sj.constant.Action.Buy and current_position < 0:
        quantity = quantity - current_position
    elif action == sj.constant.Action.Sell and current_position > 0:
        quantity = quantity + current_position

    order = api.Order(
        action=action,
        price=0.0,
        quantity=quantity,
        price_type=sj.constant.FuturesPriceType.MKT,
        order_type=sj.constant.OrderType.IOC,
        octype=sj.constant.FuturesOCType.Auto,
        account=api.futopt_account,
    )

    return api.place_order(contract, order)


def place_exit_order(api: sj.Shioaji, symbol: str, action: sj.constant.Action):
    contract = get_contract_from_symbol(api, symbol)
    current_position = get_current_position(api, contract)

    # close long
    if action == sj.constant.Action.Buy and current_position > 0:
        order = api.Order(
            action=sj.constant.Action.Sell,
            price=0.0,
            quantity=current_position,
            price_type=sj.constant.FuturesPriceType.MKT,
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Auto,
            account=api.futopt_account,
        )
        return api.place_order(contract, order)
    # close short
    elif action == sj.constant.Action.Sell and current_position < 0:
        order = api.Order(
            action=sj.constant.Action.Buy,
            price=0.0,
            quantity=-current_position,
            price_type=sj.constant.FuturesPriceType.MKT,
            order_type=sj.constant.OrderType.IOC,
            octype=sj.constant.FuturesOCType.Auto,
            account=api.futopt_account,
        )
        return api.place_order(contract, order)
    return None
