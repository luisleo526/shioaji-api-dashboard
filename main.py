import os
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field, model_validator
import shioaji as sj

from utils import get_api_client, get_valid_symbols


ACCEPT_ACTIONS = Literal["long_entry ", "long_exit", "short_entry", "short_exit"]


class OrderRequest(BaseModel):
    action: ACCEPT_ACTIONS
    quantity: int = Field(..., gt=0)
    symbol: str

    @model_validator(mode="after")
    def validate_symbol(self):
        api = get_api_client()
        if self.symbol not in get_valid_symbols(api):
            raise ValueError(f"Symbol {self.symbol} is not valid")
        return self


app = FastAPI()


@app.post("/order")
async def create_order(order_request: OrderRequest):
    api = get_api_client()

    # check if symbok exists in
