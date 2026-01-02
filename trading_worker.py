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
import threading
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

from pathlib import Path

from trading_queue import (
    TradingRequest,
    TradingResponse,
    TradingOperation,
    REQUEST_QUEUE,
    RESPONSE_PREFIX,
    REDIS_URL,
    get_queue_prefix,
)
from trading import (
    SUPPORTED_FUTURES,
    get_valid_symbols,
    get_valid_symbols_with_info,
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

# Development mock mode - bypasses Shioaji entirely
DEV_MOCK_MODE = os.getenv("DEV_MOCK_MODE", "false").lower() == "true"
if DEV_MOCK_MODE:
    logger.warning("=" * 60)
    logger.warning("DEV_MOCK_MODE ENABLED - Shioaji will NOT be used")
    logger.warning("All responses will be mock data for development")
    logger.warning("=" * 60)

MAX_RECONNECT_ATTEMPTS = 10
QUEUE_POLL_TIMEOUT = 5  # seconds to wait for queue items
HEALTH_CHECK_INTERVAL = 300  # 5 minutes - check connection health periodically
CONNECTION_LOGOUT_TIMEOUT = 3  # seconds to wait for logout before giving up


class TradingWorker:
    """
    Worker that maintains Shioaji connections and processes trading requests.

    Features:
    - Automatic reconnection on connection loss or token expiration
    - Graceful handling of SDK session disconnects
    - Periodic health checks to detect stale connections
    - Multi-tenant support via TENANT_ID environment variable
    """

    def __init__(self):
        # Multi-tenant support
        self.tenant_id = os.getenv("TENANT_ID", "")
        self.tenant_slug = os.getenv("TENANT_SLUG", "")
        self._queue_prefix = get_queue_prefix(self.tenant_id)

        # Queue names with tenant prefix
        self._request_queue = f"{self._queue_prefix}{REQUEST_QUEUE}"
        self._response_prefix = f"{self._queue_prefix}{RESPONSE_PREFIX}"

        if self.tenant_id:
            logger.info(f"Running in multi-tenant mode: tenant_id={self.tenant_id}, slug={self.tenant_slug}")
            logger.info(f"Queue prefix: {self._queue_prefix}")

        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.running = False
        self.api_clients: Dict[bool, Optional[sj.Shioaji]] = {
            True: None,   # simulation
            False: None,  # real trading
        }
        self.pending_trades: Dict[str, Any] = {}  # Store trades for status checking

        # Track connection health
        self._last_successful_request: Dict[bool, float] = {
            True: 0.0,
            False: 0.0,
        }
        self._connection_lock = threading.Lock()

        # Track if connections are being invalidated (to avoid concurrent cleanup)
        self._invalidating: Dict[bool, bool] = {
            True: False,
            False: False,
        }

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _read_secret(self, env_name: str, file_env_name: str) -> Optional[str]:
        """
        Read a secret from environment variable or file.

        Supports Docker secrets by reading from files mounted at /run/secrets.

        Args:
            env_name: Environment variable name (e.g., "API_KEY")
            file_env_name: Environment variable containing file path (e.g., "API_KEY_FILE")

        Returns:
            Secret value or None if not found
        """
        # First try direct environment variable
        value = os.getenv(env_name)
        if value:
            return value

        # Then try file-based secret (Docker secrets)
        file_path = os.getenv(file_env_name)
        if file_path:
            path = Path(file_path)
            if path.exists():
                return path.read_text().strip()
            else:
                logger.warning(f"Secret file not found: {file_path}")

        return None

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.running = False

    def _setup_event_callbacks(self, api: sj.Shioaji, simulation: bool):
        """
        Set up event callbacks for SDK session events.
        
        This helps detect session disconnections and reconnections at the SDK level.
        """
        mode_str = "simulation" if simulation else "real"
        
        try:
            @api.quote.on_event
            def event_callback(resp_code: int, event_code: int, info: str, event: str):
                """Handle SDK session events."""
                # Event codes:
                # 0 = Session up
                # 12 = Session reconnecting  
                # 13 = Session reconnected
                # 16 = Subscribe/Unsubscribe ok
                
                if event_code == 0:
                    logger.info(f"[{mode_str}] SDK session established")
                elif event_code == 12:
                    logger.warning(f"[{mode_str}] SDK session disconnected, reconnecting...")
                elif event_code == 13:
                    logger.info(f"[{mode_str}] SDK session reconnected")
                    # After SDK reconnection, we should verify the token is still valid
                    # This will be checked on the next request
                elif event_code == 16:
                    logger.debug(f"[{mode_str}] Subscribe/Unsubscribe operation completed")
                else:
                    logger.debug(f"[{mode_str}] SDK event: code={event_code}, event={event}, info={info}")
                    
            logger.debug(f"Event callbacks set up for {mode_str} connection")
        except Exception as e:
            # Event callbacks are optional - don't fail if they can't be set up
            logger.debug(f"Could not set up event callbacks: {e}")

    def _get_api_client(self, simulation: bool) -> sj.Shioaji:
        """
        Get or create an API client for the specified mode.
        Handles connection and reconnection logic.

        Credentials are read from environment variables or Docker secrets files.
        """
        if self.api_clients[simulation] is not None:
            return self.api_clients[simulation]

        # Read credentials (support both env vars and Docker secrets)
        api_key = self._read_secret("API_KEY", "API_KEY_FILE")
        secret_key = self._read_secret("SECRET_KEY", "SECRET_KEY_FILE")

        if not api_key or not secret_key:
            raise ValueError(
                "API credentials not found. Set API_KEY/SECRET_KEY environment variables "
                "or API_KEY_FILE/SECRET_KEY_FILE for Docker secrets."
            )

        mode_str = "simulation" if simulation else "real"
        logger.info(f"Creating new Shioaji connection ({mode_str} mode)...")

        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            try:
                api = sj.Shioaji(simulation=simulation)
                api.login(api_key=api_key, secret_key=secret_key)
                logger.info(f"Successfully logged in to Shioaji ({mode_str} mode)")

                # Set up event callbacks for session monitoring
                self._setup_event_callbacks(api, simulation)
                
                # Activate CA for real trading
                if not simulation:
                    self._activate_ca(api)

                self.api_clients[simulation] = api
                # Record successful connection time
                self._last_successful_request[simulation] = time.time()
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
        """Activate CA certificate for real trading.

        Supports both direct paths and Docker secrets files.
        """
        # Try file-based path first (Docker secrets)
        ca_path = self._read_secret("CA_PATH", "CA_PATH_FILE")
        ca_password = self._read_secret("CA_PASSWORD", "CA_PASSWORD_FILE")

        if not ca_path or not ca_password:
            logger.warning(
                "CA_PATH/CA_PASSWORD or CA_PATH_FILE/CA_PASSWORD_FILE not set, "
                "skipping CA activation"
            )
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
        """
        Invalidate a connection (e.g., after error) to force reconnection.
        
        This method handles the case where the connection is already dead
        and logout might timeout or fail.
        """
        mode_str = "simulation" if simulation else "real"
        
        # Prevent concurrent invalidation
        if self._invalidating[simulation]:
            logger.debug(f"Already invalidating {mode_str} connection, skipping...")
            return
            
        with self._connection_lock:
            if self.api_clients[simulation] is None:
                logger.debug(f"No {mode_str} connection to invalidate")
                return
                
            self._invalidating[simulation] = True
            logger.warning(f"Invalidating {mode_str} connection...")
            
            # Get reference to old client and immediately clear our reference
            # This prevents the garbage collector from trying to logout later
            old_api = self.api_clients[simulation]
            self.api_clients[simulation] = None
            
            # Try to logout gracefully, but don't block for too long
            try:
                # Use a thread to attempt logout with timeout
                logout_done = threading.Event()
                logout_error = [None]
                
                def do_logout():
                    try:
                        old_api.logout()
                    except Exception as e:
                        logout_error[0] = e
                    finally:
                        logout_done.set()
                
                logout_thread = threading.Thread(target=do_logout, daemon=True)
                logout_thread.start()
                
                # Wait for logout with timeout
                if logout_done.wait(timeout=CONNECTION_LOGOUT_TIMEOUT):
                    if logout_error[0]:
                        logger.debug(f"Logout completed with error: {logout_error[0]}")
                    else:
                        logger.debug(f"Logout completed successfully")
                else:
                    logger.warning(
                        f"Logout timed out after {CONNECTION_LOGOUT_TIMEOUT}s, "
                        f"abandoning old {mode_str} connection"
                    )
                    # Don't wait for the thread - it's a daemon thread and will be
                    # cleaned up when the process exits
                    
            except Exception as e:
                logger.debug(f"Error during logout attempt: {e}")
            finally:
                self._invalidating[simulation] = False
                logger.info(f"{mode_str.capitalize()} connection invalidated, will reconnect on next request")

    def _check_connection_health(self, simulation: bool) -> bool:
        """
        Check if an existing connection is still healthy.
        
        This performs a lightweight check to verify the connection is still valid.
        Returns True if healthy, False if the connection should be invalidated.
        """
        mode_str = "simulation" if simulation else "real"
        api = self.api_clients.get(simulation)
        
        if api is None:
            return False
            
        try:
            # Try to list accounts - this is a lightweight API call that validates the token
            accounts = api.list_accounts()
            if accounts:
                logger.debug(f"{mode_str.capitalize()} connection health check passed")
                self._last_successful_request[simulation] = time.time()
                return True
            else:
                logger.warning(f"{mode_str.capitalize()} connection health check: no accounts returned")
                return False
        except (TokenError, SystemMaintenance, SjTimeoutError) as e:
            logger.warning(f"{mode_str.capitalize()} connection health check failed: {e}")
            return False
        except Exception as e:
            error_str = str(e).lower()
            if "token" in error_str or "expired" in error_str or "401" in error_str:
                logger.warning(f"{mode_str.capitalize()} connection health check failed: {e}")
                return False
            # For other errors, assume connection might still be OK
            logger.debug(f"{mode_str.capitalize()} connection health check had error: {e}")
            return True

    def _maybe_refresh_connection(self, simulation: bool):
        """
        Check if connection needs to be refreshed and invalidate if necessary.
        
        This is called periodically to proactively detect stale connections.
        """
        mode_str = "simulation" if simulation else "real"
        
        if self.api_clients.get(simulation) is None:
            return  # No connection to refresh
            
        last_success = self._last_successful_request[simulation]
        time_since_success = time.time() - last_success
        
        # If it's been a while since successful request, verify connection is still healthy
        if time_since_success > HEALTH_CHECK_INTERVAL:
            logger.info(f"Checking {mode_str} connection health (last success: {time_since_success:.0f}s ago)...")
            if not self._check_connection_health(simulation):
                logger.warning(f"{mode_str.capitalize()} connection appears stale, invalidating...")
                self._invalidate_connection(simulation)

    def _handle_mock_request(self, request: TradingRequest) -> TradingResponse:
        """Handle request with mock data (for development without Shioaji)."""
        import random
        operation = request.operation
        params = request.params

        logger.debug(f"[MOCK] Processing request: {operation}")

        if operation == TradingOperation.PING.value:
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={"status": "healthy", "simulation": request.simulation, "mock": True},
            )

        elif operation == TradingOperation.GET_SYMBOLS.value:
            # Return mock symbols for supported futures
            mock_symbols = []
            for product in ["MXF", "TXF"]:
                mock_symbols.append({
                    "symbol": product,
                    "code": f"{product}F5",
                    "name": f"{product} 近月",
                    "category": "Futures",
                    "delivery_month": "202501",
                })
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={"symbols": mock_symbols, "count": len(mock_symbols)},
            )

        elif operation == TradingOperation.GET_SYMBOL_INFO.value:
            symbol = params.get("symbol", "MXF")
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "symbol": symbol,
                    "code": f"{symbol}F5",
                    "name": f"{symbol} 近月",
                    "category": "Futures",
                    "delivery_month": "202501",
                    "underlying_kind": "I",
                    "limit_up": 25000.0,
                    "limit_down": 20000.0,
                    "reference": 22500.0,
                },
            )

        elif operation == TradingOperation.GET_CONTRACT_CODES.value:
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={"contracts": ["MXFF5", "TXFF5", "MXFG5", "TXFG5"], "count": 4},
            )

        elif operation == TradingOperation.GET_POSITIONS.value:
            # Return empty positions by default, or mock positions if configured
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={"positions": [], "count": 0},
            )

        elif operation == TradingOperation.GET_FUTURES_OVERVIEW.value:
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "products": [
                        {"product": "MXF", "contracts": [{"symbol": "MXF", "name": "小型台指期貨", "code": "MXFF5"}], "count": 1},
                        {"product": "TXF", "contracts": [{"symbol": "TXF", "name": "台指期貨", "code": "TXFF5"}], "count": 1},
                    ]
                },
            )

        elif operation == TradingOperation.GET_PRODUCT_CONTRACTS.value:
            product = params.get("product", "MXF").upper()
            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "product": product,
                    "contracts": [
                        {"symbol": product, "code": f"{product}F5", "name": f"{product} 近月", "delivery_month": "202501", "category": "Futures"},
                        {"symbol": product, "code": f"{product}G5", "name": f"{product} 次月", "delivery_month": "202502", "category": "Futures"},
                    ],
                    "count": 2,
                },
            )

        elif operation == TradingOperation.PLACE_ENTRY_ORDER.value:
            symbol = params.get("symbol", "MXF")
            quantity = params.get("quantity", 1)
            action = params.get("action", "Buy")
            order_id = f"mock-{int(time.time() * 1000)}"

            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "order_id": order_id,
                    "seqno": f"{random.randint(100000, 999999)}",
                    "ordno": f"M{random.randint(100000, 999999)}",
                    "action": action,
                    "quantity": quantity,
                    "original_quantity": quantity,
                    "symbol": symbol,
                    "code": f"{symbol}F5",
                    "mock": True,
                },
            )

        elif operation == TradingOperation.PLACE_EXIT_ORDER.value:
            symbol = params.get("symbol", "MXF")
            order_id = f"mock-{int(time.time() * 1000)}"

            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "order_id": order_id,
                    "seqno": f"{random.randint(100000, 999999)}",
                    "ordno": f"M{random.randint(100000, 999999)}",
                    "action": "Sell",
                    "quantity": 1,
                    "symbol": symbol,
                    "code": f"{symbol}F5",
                    "mock": True,
                },
            )

        elif operation == TradingOperation.CHECK_ORDER_STATUS.value:
            order_id = params.get("order_id", "")
            seqno = params.get("seqno", "")

            return TradingResponse(
                request_id=request.request_id,
                success=True,
                data={
                    "status": "Filled",
                    "order_id": order_id,
                    "seqno": seqno,
                    "ordno": f"M{random.randint(100000, 999999)}",
                    "order_quantity": 1,
                    "deal_quantity": 1,
                    "cancel_quantity": 0,
                    "fill_avg_price": 22500.0,
                    "deals": [{"seq": "1", "price": 22500.0, "quantity": 1, "ts": int(time.time())}],
                    "mock": True,
                },
            )

        else:
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=f"Unknown operation: {operation}",
            )

    def _handle_request(self, request: TradingRequest) -> TradingResponse:
        """Process a single trading request."""
        # Use mock handler if in development mock mode
        if DEV_MOCK_MODE:
            return self._handle_mock_request(request)

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
                symbols_info = get_valid_symbols_with_info(api)
                return TradingResponse(
                    request_id=request.request_id,
                    success=True,
                    data={"symbols": symbols_info, "count": len(symbols_info)},
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
                
                # Build code-to-symbol mapping from ALL futures contracts
                code_to_symbol = {}
                for product_name in dir(api.Contracts.Futures):
                    if product_name.startswith("_"):
                        continue
                    product = getattr(api.Contracts.Futures, product_name)
                    if hasattr(product, "__iter__"):
                        for contract in product:
                            if hasattr(contract, "code") and hasattr(contract, "symbol"):
                                code_to_symbol[contract.code] = contract.symbol
                
                positions_data = []
                for p in positions:
                    # Look up symbol from code (fallback to code if not found)
                    symbol = code_to_symbol.get(p.code, p.code)
                    
                    positions_data.append({
                        "id": getattr(p, "id", ""),
                        "symbol": symbol,
                        "code": p.code,
                        "direction": str(p.direction.value) if hasattr(p.direction, 'value') else str(p.direction),
                        "quantity": p.quantity,
                        "price": p.price,
                        "last_price": getattr(p, "last_price", p.price),
                        "pnl": p.pnl,
                        "yd_quantity": getattr(p, "yd_quantity", 0),
                        "cond": getattr(p, "cond", ""),
                    })
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

        except (TokenError, SystemMaintenance, SjTimeoutError) as e:
            # These errors indicate the connection is no longer valid
            error_type = type(e).__name__
            logger.error(f"Connection error ({error_type}): {e}, invalidating connection...")
            self._invalidate_connection(simulation)
            return TradingResponse(
                request_id=request.request_id,
                success=False,
                error=f"Connection error ({error_type}): {e}",
            )

        except Exception as e:
            error_str = str(e)
            # Check for common connection-related error patterns in exception message
            connection_error_patterns = [
                "token is expired",
                "token expired", 
                "status_code': 401",
                "statuscode: 401",
                "not ready",
                "session down",
                "connection refused",
                "connection reset",
            ]
            is_connection_error = any(
                pattern in error_str.lower() 
                for pattern in connection_error_patterns
            )
            
            if is_connection_error:
                logger.error(f"Detected connection error in exception: {e}, invalidating connection...")
                self._invalidate_connection(simulation)
                return TradingResponse(
                    request_id=request.request_id,
                    success=False,
                    error=f"Connection error: {e}",
                )
            
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
                    "symbol": contract.symbol,
                    "code": contract.code,
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
                    "symbol": contract.symbol,
                    "code": contract.code,
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

        if DEV_MOCK_MODE:
            logger.info("Running in DEV_MOCK_MODE - skipping Shioaji connection")

        self.running = True

        # Initial connection attempt (skip in mock mode)
        if not DEV_MOCK_MODE:
            try:
                self._get_api_client(simulation=True)
                logger.info("Initial simulation connection established")
            except Exception as e:
                logger.warning(f"Initial simulation connection failed: {e}")

        logger.info(f"Listening for requests on queue: {self._request_queue}")

        last_health_check = time.time()

        while self.running:
            try:
                # Block waiting for request with timeout
                result = self.redis.blpop(self._request_queue, timeout=QUEUE_POLL_TIMEOUT)

                if result is None:
                    # Timeout - good time to check connection health
                    current_time = time.time()
                    if current_time - last_health_check > HEALTH_CHECK_INTERVAL:
                        logger.debug("Periodic health check during idle...")
                        for sim_mode in [True, False]:
                            if self.api_clients.get(sim_mode) is not None:
                                self._maybe_refresh_connection(sim_mode)
                        last_health_check = current_time
                    continue

                _, request_data = result
                request = TradingRequest.from_json(request_data)

                logger.info(f"Received request: {request.operation} (id={request.request_id[:8]}...)")

                # Process request
                response = self._handle_request(request)
                
                # Track successful requests for health monitoring
                if response.success:
                    self._last_successful_request[request.simulation] = time.time()

                # Send response
                response_key = f"{self._response_prefix}{request.request_id}"
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

        # Cleanup - use _invalidate_connection for proper cleanup with timeout handling
        logger.info("Shutting down trading worker...")
        for simulation in [True, False]:
            if self.api_clients.get(simulation) is not None:
                mode = "simulation" if simulation else "real"
                logger.info(f"Cleaning up {mode} connection...")
                self._invalidate_connection(simulation)

        logger.info("Trading worker stopped")


if __name__ == "__main__":
    worker = TradingWorker()
    worker.run()

