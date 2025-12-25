#!/usr/bin/env python3
"""
Trading Worker - Dedicated Shioaji connection handler.

This worker maintains persistent connections to Shioaji (one for simulation,
one for real trading) and processes trading requests from the Redis queue.

Features:
- Single connection point for all Shioaji operations
- Automatic reconnection on connection loss
- Graceful shutdown handling
- Health monitoring
"""
import json
import logging
import os
import signal
import sys
import time
from typing import Optional, Dict, Any

import redis
import shioaji as sj
from shioaji.error import (
    TokenError,
    SystemMaintenance,
    TimeoutError as SjTimeoutError,
    AccountNotSignError,
    AccountNotProvideError,
    TargetContractNotExistError,
)

from trading_queue import (
    TradingRequest,
    TradingResponse,
    TradingOperation,
    REQUEST_QUEUE,
    RESPONSE_PREFIX,
    REDIS_URL,
)
from trading import (
    SUPPORTED_FUTURES,
    get_valid_symbols,
    get_valid_contract_codes,
    get_contract_from_symbol,
    get_current_position,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Connection settings
RECONNECT_DELAY = 5  # seconds between reconnection attempts
MAX_RECONNECT_ATTEMPTS = 10
QUEUE_POLL_TIMEOUT = 5  # seconds to wait for queue items


class TradingWorker:
    """
    Worker that maintains Shioaji connections and processes trading requests.
    """

    def __init__(self):
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.running = False
        self.api_clients: Dict[bool, Optional[sj.Shioaji]] = {
            True: None,   # simulation
            False: None,  # real trading
        }
        self.pending_trades: Dict[str, Any] = {}  # Store trades for status checking

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.running = False

    def _get_api_client(self, simulation: bool) -> sj.Shioaji:
        """
        Get or create an API client for the specified mode.
        Handles connection and reconnection logic.
        """
        if self.api_clients[simulation] is not None:
            return self.api_clients[simulation]

        api_key = os.getenv("API_KEY")
        secret_key = os.getenv("SECRET_KEY")

        if not api_key or not secret_key:
            raise ValueError("API_KEY or SECRET_KEY environment variable not set")

        mode_str = "simulation" if simulation else "real"
        logger.info(f"Creating new Shioaji connection ({mode_str} mode)...")

        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            try:
                api = sj.Shioaji(simulation=simulation)
                api.login(api_key=api_key, secret_key=secret_key)
                logger.info(f"Successfully logged in to Shioaji ({mode_str} mode)")

                # Activate CA for real trading
                if not simulation:
                    self._activate_ca(api)

                self.api_clients[simulation] = api
                return api

            except (TokenError, SystemMaintenance, SjTimeoutError) as e:
                logger.error(f"Login attempt {attempt} failed: {e}")
                if attempt < MAX_RECONNECT_ATTEMPTS:
                    logger.info(f"Retrying in {RECONNECT_DELAY}s...")
                    time.sleep(RECONNECT_DELAY)
                else:
                    raise

            except Exception as e:
                logger.error(f"Unexpected error during login: {e}")
                if attempt < MAX_RECONNECT_ATTEMPTS:
                    logger.info(f"Retrying in {RECONNECT_DELAY}s...")
                    time.sleep(RECONNECT_DELAY)
                else:
                    raise

        raise RuntimeError("Failed to connect to Shioaji after max attempts")

    def _activate_ca(self, api: sj.Shioaji):
        """Activate CA certificate for real trading."""
        ca_path = os.getenv("CA_PATH")
        ca_password = os.getenv("CA_PASSWORD")

        if not ca_path or not ca_password:
            logger.warning("CA_PATH or CA_PASSWORD not set, skipping CA activation")
            return

        accounts = api.list_accounts()
        if not accounts:
            raise ValueError("No accounts found after login")

        person_id = accounts[0].person_id
        logger.info(f"Activating CA certificate for person_id={person_id}")

        result = api.activate_ca(
            ca_path=ca_path,
            ca_passwd=ca_password,
            person_id=person_id,
        )
        logger.info(f"CA activation result: {result}")

    def _invalidate_connection(self, simulation: bool):
        """Invalidate a connection (e.g., after error) to force reconnection."""
        mode_str = "simulation" if simulation else "real"
        logger.warning(f"Invalidating {mode_str} connection...")

        if self.api_clients[simulation] is not None:
            try:
                self.api_clients[simulation].logout()
            except Exception as e:
                logger.debug(f"Error during logout: {e}")
            self.api_clients[simulation] = None

    def _handle_request(self, request: TradingRequest) -> TradingResponse:
        """Process a single trading request."""
        operation = request.operation
        simulation = request.simulation
        params = request.params

        logger.debug(f"Processing request: {operation} (simulation={simulation})")

        try:
            api = self._get_api_client(simulation)

            if operation == TradingOperation.PING.value:
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"status": "healthy", "simulation": simulation},
                )

            elif operation == TradingOperation.GET_SYMBOLS.value:
                symbols = get_valid_symbols(api)
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"symbols": symbols, "count": len(symbols)},
                )

            elif operation == TradingOperation.GET_SYMBOL_INFO.value:
                symbol = params["symbol"]
                contract = get_contract_from_symbol(api, symbol)
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={
                        "symbol": contract.symbol,
                        "code": contract.code,
                        "name": contract.name,
                        "category": contract.category,
                        "delivery_month": contract.delivery_month,
                        "underlying_kind": contract.underlying_kind,
                        "limit_up": contract.limit_up,
                        "limit_down": contract.limit_down,
                        "reference": contract.reference,
                    },
                )

            elif operation == TradingOperation.GET_CONTRACT_CODES.value:
                codes = get_valid_contract_codes(api)
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"contracts": codes, "count": len(codes)},
                )

            elif operation == TradingOperation.GET_POSITIONS.value:
                positions = api.list_positions(api.futopt_account)
                positions_data = [
                    {
                        "id": getattr(p, "id", ""),
                        "code": p.code,
                        "direction": str(p.direction.value) if hasattr(p.direction, 'value') else str(p.direction),
                        "quantity": p.quantity,
                        "price": p.price,
                        "last_price": getattr(p, "last_price", p.price),
                        "pnl": p.pnl,
                        "yd_quantity": getattr(p, "yd_quantity", 0),
                        "cond": getattr(p, "cond", ""),
                    }
                    for p in positions
                ]
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"positions": positions_data, "count": len(positions_data)},
                )

            elif operation == TradingOperation.GET_FUTURES_OVERVIEW.value:
                futures = api.Contracts.Futures
                products = []
                for product_name in dir(futures):
                    if product_name.startswith("_"):
                        continue
                    product = getattr(futures, product_name)
                    if hasattr(product, "__iter__"):
                        contracts = [
                            {"symbol": c.symbol, "name": c.name, "code": c.code}
                            for c in product
                            if hasattr(c, "symbol")
                        ]
                        if contracts:
                            products.append({
                                "product": product_name,
                                "contracts": contracts,
                                "count": len(contracts),
                            })
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"products": products},
                )

            elif operation == TradingOperation.GET_PRODUCT_CONTRACTS.value:
                product = params["product"].upper()
                product_contracts = getattr(api.Contracts.Futures, product, None)
                if not product_contracts:
                    return TradingResponse(
                        request_id=request.request_id,
                        success=False,
                        error=f"Product '{product}' not found",
                    )
                contracts = [
                    {
                        "symbol": c.symbol,
                        "code": c.code,
                        "name": c.name,
                        "delivery_month": c.delivery_month,
                        "category": c.category,
                    }
                    for c in product_contracts
                    if hasattr(c, "symbol")
                ]
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"product": product, "contracts": contracts, "count": len(contracts)},
                )

            elif operation == TradingOperation.PLACE_ENTRY_ORDER.value:
                return self._handle_entry_order(api, request)

            elif operation == TradingOperation.PLACE_EXIT_ORDER.value:
                return self._handle_exit_order(api, request)

            elif operation == TradingOperation.CHECK_ORDER_STATUS.value:
                return self._handle_check_order_status(api, request)

            else:
                return TradingResponse(
                    request_id=request.request_id,
                    success=False,
                    error=f"Unknown operation: {operation}",
                )

        except (TokenError, SystemMaintenance) as e:
            logger.error(f"Connection error: {e}, invalidating connection...")
            self._invalidate_connection(simulation)
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=f"Connection error: {e}",
            )

        except Exception as e:
            logger.exception(f"Error processing request: {e}")
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=str(e),
            )

    def _handle_entry_order(self, api: sj.Shioaji, request: TradingRequest) -> TradingResponse:
        """Handle entry order placement."""
        params = request.params
        symbol = params["symbol"]
        quantity = params["quantity"]
        action_str = params["action"]

        action = sj.constant.Action.Buy if action_str == "Buy" else sj.constant.Action.Sell

        try:
            contract = get_contract_from_symbol(api, symbol)
            current_position = get_current_position(api, contract) or 0

            # Adjust quantity for position reversal
            original_quantity = quantity
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

            result = api.place_order(contract, order)

            # Store trade for later status checking
            trade_key = f"{result.order.id}:{result.order.seqno}"
            self.pending_trades[trade_key] = result

            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "order_id": result.order.id,
                    "seqno": result.order.seqno,
                    "ordno": getattr(result.order, "ordno", ""),
                    "action": action_str,
                    "quantity": quantity,
                    "original_quantity": original_quantity,
                    "symbol": symbol,
                },
            )

        except (TargetContractNotExistError, AccountNotSignError, AccountNotProvideError) as e:
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=str(e),
            )

    def _handle_exit_order(self, api: sj.Shioaji, request: TradingRequest) -> TradingResponse:
        """Handle exit order placement."""
        params = request.params
        symbol = params["symbol"]
        position_direction = params["position_direction"]

        direction = (
            sj.constant.Action.Buy
            if position_direction == "Buy"
            else sj.constant.Action.Sell
        )

        try:
            contract = get_contract_from_symbol(api, symbol)
            current_position = get_current_position(api, contract) or 0

            # Determine exit action and quantity
            if direction == sj.constant.Action.Buy and current_position > 0:
                action = sj.constant.Action.Sell
                quantity = current_position
            elif direction == sj.constant.Action.Sell and current_position < 0:
                action = sj.constant.Action.Buy
                quantity = -current_position
            else:
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"message": "No position to exit", "order_id": None},
                )

            order = api.Order(
                action=action,
                price=0.0,
                quantity=quantity,
                price_type=sj.constant.FuturesPriceType.MKT,
                order_type=sj.constant.OrderType.IOC,
                octype=sj.constant.FuturesOCType.Auto,
                account=api.futopt_account,
            )

            result = api.place_order(contract, order)

            # Store trade for later status checking
            trade_key = f"{result.order.id}:{result.order.seqno}"
            self.pending_trades[trade_key] = result

            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "order_id": result.order.id,
                    "seqno": result.order.seqno,
                    "ordno": getattr(result.order, "ordno", ""),
                    "action": action.value if hasattr(action, "value") else str(action),
                    "quantity": quantity,
                    "symbol": symbol,
                },
            )

        except (TargetContractNotExistError, AccountNotSignError, AccountNotProvideError) as e:
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=str(e),
            )

    def _handle_check_order_status(self, api: sj.Shioaji, request: TradingRequest) -> TradingResponse:
        """Handle order status check."""
        params = request.params
        order_id = params["order_id"]
        seqno = params["seqno"]

        trade_key = f"{order_id}:{seqno}"
        trade = self.pending_trades.get(trade_key)

        if not trade:
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=f"Trade not found: {trade_key}",
            )

        try:
            api.update_status(trade=trade)

            status_obj = trade.status
            status_value = (
                status_obj.status.value
                if hasattr(status_obj.status, "value")
                else str(status_obj.status)
            )

            deals = status_obj.deals if status_obj.deals else []
            deal_quantity = getattr(status_obj, "deal_quantity", 0)

            total_value = sum(d.price * d.quantity for d in deals) if deals else 0
            total_qty = sum(d.quantity for d in deals) if deals else 0
            fill_avg_price = total_value / total_qty if total_qty > 0 else 0.0

            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "status": status_value,
                    "order_id": order_id,
                    "seqno": seqno,
                    "ordno": getattr(trade.order, "ordno", ""),
                    "order_quantity": getattr(status_obj, "order_quantity", 0),
                    "deal_quantity": deal_quantity,
                    "cancel_quantity": getattr(status_obj, "cancel_quantity", 0),
                    "fill_avg_price": fill_avg_price,
                    "deals": [
                        {
                            "seq": getattr(d, "seq", ""),
                            "price": d.price,
                            "quantity": d.quantity,
                            "ts": getattr(d, "ts", 0),
                        }
                        for d in deals
                    ],
                },
            )

        except Exception as e:
            logger.exception(f"Error checking order status: {e}")
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=str(e),
            )

    def run(self):
        """Main loop - process requests from the queue."""
        logger.info("Trading worker starting...")
        logger.info(f"Supported futures: {SUPPORTED_FUTURES}")

        self.running = True

        # Initial connection attempt
        try:
            self._get_api_client(simulation=True)
            logger.info("Initial simulation connection established")
        except Exception as e:
            logger.warning(f"Initial simulation connection failed: {e}")

        logger.info(f"Listening for requests on queue: {REQUEST_QUEUE}")

        while self.running:
            try:
                # Block waiting for request with timeout
                result = self.redis.blpop(REQUEST_QUEUE, timeout=QUEUE_POLL_TIMEOUT)

                if result is None:
                    continue  # Timeout, check if still running

                _, request_data = result
                request = TradingRequest.from_json(request_data)

                logger.info(f"Received request: {request.operation} (id={request.request_id[:8]}...)")

                # Process request
                response = self._handle_request(request)

                # Send response
                response_key = f"{RESPONSE_PREFIX}{request.request_id}"
                self.redis.rpush(response_key, response.to_json())
                self.redis.expire(response_key, 60)  # Clean up after 60s

                logger.info(
                    f"Completed request: {request.operation} "
                    f"(success={response.success}, id={request.request_id[:8]}...)"
                )

            except redis.ConnectionError as e:
                logger.error(f"Redis connection error: {e}")
                time.sleep(RECONNECT_DELAY)

            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                time.sleep(1)

        # Cleanup
        logger.info("Shutting down trading worker...")
        for simulation, api in self.api_clients.items():
            if api is not None:
                try:
                    api.logout()
                    mode = "simulation" if simulation else "real"
                    logger.info(f"Logged out from Shioaji ({mode} mode)")
                except Exception as e:
                    logger.debug(f"Error during logout: {e}")

        logger.info("Trading worker stopped")


if __name__ == "__main__":
    worker = TradingWorker()
    worker.run()

