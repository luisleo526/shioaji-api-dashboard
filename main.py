import os
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
import shioaji as sj

from utils import (
    get_api_client,
    get_valid_symbols,
    place_entry_order,
    place_exit_order,
    LoginError,
    OrderError,
)


ACCEPT_ACTIONS = Literal["long_entry", "long_exit", "short_entry", "short_exit"]


class OrderRequest(BaseModel):
    action: ACCEPT_ACTIONS
    quantity: int = Field(..., gt=0)
    symbol: str

    @model_validator(mode="after")
    def validate_symbol(self):
        try:
            api = get_api_client()
            if self.symbol not in get_valid_symbols(api):
                raise ValueError(f"Symbol {self.symbol} is not valid")
        except LoginError as e:
            raise ValueError(f"Failed to validate symbol: {e}")
        return self


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/order")
async def create_order(order_request: OrderRequest):
    try:
        api = get_api_client()
    except LoginError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        if order_request.action == "long_entry":
            result = place_entry_order(
                api, order_request.symbol, order_request.quantity, sj.constant.Action.Buy
            )
        elif order_request.action == "short_entry":
            result = place_entry_order(
                api, order_request.symbol, order_request.quantity, sj.constant.Action.Sell
            )
        elif order_request.action == "long_exit":
            result = place_exit_order(
                api, order_request.symbol, sj.constant.Action.Buy
            )
        elif order_request.action == "short_exit":
            result = place_exit_order(
                api, order_request.symbol, sj.constant.Action.Sell
            )
    except OrderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result is None:
        return {"status": "no_action", "message": "No position to exit or invalid action"}

    return {"status": "success", "order": str(result)}