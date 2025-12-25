from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Float, Enum
import enum

from database import Base


class OrderAction(str, enum.Enum):
    LONG_ENTRY = "long_entry"
    LONG_EXIT = "long_exit"
    SHORT_ENTRY = "short_entry"
    SHORT_EXIT = "short_exit"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL_FILLED = "partial_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    NO_ACTION = "no_action"


class OrderHistory(Base):
    __tablename__ = "order_history"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String, nullable=False)
    order_result = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Order tracking fields (from Shioaji Trade object)
    order_id = Column(String, nullable=True, index=True)  # Trade.order.id
    seqno = Column(String, nullable=True)  # Trade.order.seqno
    ordno = Column(String, nullable=True)  # Trade.order.ordno
    
    # Fill tracking fields
    fill_status = Column(String, nullable=True)  # Status from exchange: PendingSubmit, Submitted, Filled, etc.
    fill_quantity = Column(Integer, nullable=True)  # Actual filled quantity
    fill_price = Column(Float, nullable=True)  # Average fill price
    cancel_quantity = Column(Integer, nullable=True)  # Cancelled quantity
    updated_at = Column(DateTime, nullable=True)  # Last status update time

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "status": self.status,
            "order_result": self.order_result,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "order_id": self.order_id,
            "fill_status": self.fill_status,
            "fill_quantity": self.fill_quantity,
            "fill_price": self.fill_price,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

