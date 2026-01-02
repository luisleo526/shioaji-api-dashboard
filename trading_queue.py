"""
Trading Queue - Redis-based communication interface for Shioaji operations.

This module provides a request/response pattern using Redis for communication
between FastAPI workers and the dedicated trading worker that maintains
the Shioaji connection.
"""
import json
import logging
import os
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Optional
from enum import Enum

import redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TENANT_ID = os.getenv("TENANT_ID", "")  # Multi-tenant support
REQUEST_QUEUE = "trading:requests"
RESPONSE_PREFIX = "trading:response:"
REQUEST_TIMEOUT = 30  # seconds to wait for response


def get_queue_prefix(tenant_id: Optional[str] = None) -> str:
    """Get the queue prefix for a tenant."""
    tid = tenant_id or TENANT_ID
    return f"tenant:{tid}:" if tid else ""


class TradingOperation(str, Enum):
    """Supported trading operations."""
    GET_SYMBOLS = "get_symbols"
    GET_SYMBOL_INFO = "get_symbol_info"
    GET_CONTRACT_CODES = "get_contract_codes"
    GET_POSITIONS = "get_positions"
    GET_FUTURES_OVERVIEW = "get_futures_overview"
    GET_PRODUCT_CONTRACTS = "get_product_contracts"
    PLACE_ENTRY_ORDER = "place_entry_order"
    PLACE_EXIT_ORDER = "place_exit_order"
    CHECK_ORDER_STATUS = "check_order_status"
    PING = "ping"


@dataclass
class TradingRequest:
    """Request message for trading operations."""
    request_id: str
    operation: str
    simulation: bool
    params: dict

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TradingRequest":
        d = json.loads(data)
        return cls(**d)


@dataclass
class TradingResponse:
    """Response message from trading operations."""
    request_id: str
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "TradingResponse":
        d = json.loads(data)
        return cls(**d)


class TradingQueueClient:
    """
    Client for submitting trading requests to the queue.
    Used by FastAPI workers to communicate with the trading worker.

    Supports multi-tenant mode via tenant_id parameter.
    """

    def __init__(self, redis_url: str = REDIS_URL, tenant_id: Optional[str] = None):
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.tenant_id = tenant_id
        self._prefix = get_queue_prefix(tenant_id)
        self._request_queue = f"{self._prefix}{REQUEST_QUEUE}"
        self._response_prefix = f"{self._prefix}{RESPONSE_PREFIX}"
        self._check_connection()

    def _check_connection(self):
        """Verify Redis connection is working."""
        try:
            self.redis.ping()
            logger.debug("Redis connection established")
        except redis.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            raise

    def submit_request(
        self,
        operation: TradingOperation,
        simulation: bool = True,
        params: Optional[dict] = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> TradingResponse:
        """
        Submit a trading request and wait for response.

        Args:
            operation: The trading operation to perform
            simulation: Whether to use simulation mode
            params: Operation-specific parameters
            timeout: Seconds to wait for response

        Returns:
            TradingResponse with the result

        Raises:
            TimeoutError: If no response received within timeout
            ConnectionError: If Redis connection fails
        """
        request_id = str(uuid.uuid4())
        request = TradingRequest(
            request_id=request_id,
            operation=operation.value,
            simulation=simulation,
            params=params or {},
        )

        response_key = f"{self._response_prefix}{request_id}"

        try:
            # Push request to queue
            self.redis.rpush(self._request_queue, request.to_json())
            logger.debug(f"Submitted request {request_id}: {operation.value}")

            # Wait for response with blocking pop
            result = self.redis.blpop(response_key, timeout=timeout)

            if result is None:
                logger.error(f"Request {request_id} timed out after {timeout}s")
                raise TimeoutError(f"Trading request timed out after {timeout}s")

            _, response_data = result
            response = TradingResponse.from_json(response_data)
            logger.debug(f"Received response for {request_id}: success={response.success}")

            return response

        except redis.ConnectionError as e:
            logger.error(f"Redis connection error: {e}")
            raise ConnectionError(f"Failed to communicate with trading queue: {e}")

    def check_worker_health(self) -> bool:
        """Check if the trading worker is healthy by sending a ping."""
        try:
            response = self.submit_request(
                TradingOperation.PING,
                simulation=True,
                timeout=5,
            )
            return response.success
        except (TimeoutError, ConnectionError):
            return False

    def get_symbols(self, simulation: bool = True) -> TradingResponse:
        """Get valid trading symbols."""
        return self.submit_request(TradingOperation.GET_SYMBOLS, simulation)

    def get_symbol_info(self, symbol: str, simulation: bool = True) -> TradingResponse:
        """Get detailed info for a specific symbol."""
        return self.submit_request(
            TradingOperation.GET_SYMBOL_INFO,
            simulation,
            params={"symbol": symbol},
        )

    def get_contract_codes(self, simulation: bool = True) -> TradingResponse:
        """Get valid contract codes."""
        return self.submit_request(TradingOperation.GET_CONTRACT_CODES, simulation)

    def get_positions(self, simulation: bool = True) -> TradingResponse:
        """Get current positions."""
        return self.submit_request(TradingOperation.GET_POSITIONS, simulation)

    def get_futures_overview(self, simulation: bool = True) -> TradingResponse:
        """Get overview of all futures products."""
        return self.submit_request(TradingOperation.GET_FUTURES_OVERVIEW, simulation)

    def get_product_contracts(
        self, product: str, simulation: bool = True
    ) -> TradingResponse:
        """Get all contracts for a specific product."""
        return self.submit_request(
            TradingOperation.GET_PRODUCT_CONTRACTS,
            simulation,
            params={"product": product},
        )

    def place_entry_order(
        self,
        symbol: str,
        quantity: int,
        action: str,
        simulation: bool = True,
    ) -> TradingResponse:
        """Place an entry order."""
        return self.submit_request(
            TradingOperation.PLACE_ENTRY_ORDER,
            simulation,
            params={"symbol": symbol, "quantity": quantity, "action": action},
        )

    def place_exit_order(
        self,
        symbol: str,
        position_direction: str,
        simulation: bool = True,
    ) -> TradingResponse:
        """Place an exit order."""
        return self.submit_request(
            TradingOperation.PLACE_EXIT_ORDER,
            simulation,
            params={"symbol": symbol, "position_direction": position_direction},
        )

    def check_order_status(
        self,
        order_id: str,
        seqno: str,
        simulation: bool = True,
    ) -> TradingResponse:
        """Check status of an order."""
        return self.submit_request(
            TradingOperation.CHECK_ORDER_STATUS,
            simulation,
            params={"order_id": order_id, "seqno": seqno},
            timeout=60,  # Order status checks may take longer
        )


# Singleton instance for FastAPI workers
_queue_client: Optional[TradingQueueClient] = None


def get_queue_client() -> TradingQueueClient:
    """Get or create the singleton queue client."""
    global _queue_client
    if _queue_client is None:
        _queue_client = TradingQueueClient()
    return _queue_client

