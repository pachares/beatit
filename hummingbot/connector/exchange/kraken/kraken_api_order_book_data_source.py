#!/usr/bin/env python

import asyncio
import logging
from typing import (
    Any,
    AsyncIterable,
    Dict,
    List,
    Optional
)
import time

import aiohttp
import pandas as pd
import ujson
import websockets
from websockets.exceptions import ConnectionClosed

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.logger import HummingbotLogger
from hummingbot.connector.exchange.kraken.kraken_order_book import KrakenOrderBook
from hummingbot.connector.exchange.kraken.kraken_utils import (
    convert_from_exchange_trading_pair,
    convert_to_exchange_trading_pair)
from hummingbot.connector.exchange.kraken import kraken_constants as CONSTANTS


class KrakenAPIOrderBookDataSource(OrderBookTrackerDataSource):

    MESSAGE_TIMEOUT = 30.0
    PING_TIMEOUT = 10.0

    _kraobds_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._kraobds_logger is None:
            cls._kraobds_logger = logging.getLogger(__name__)
        return cls._kraobds_logger

    def __init__(self, throttler: Optional[AsyncThrottler] = None, trading_pairs: List[str] = None):
        super().__init__(trading_pairs)
        self._order_book_create_function = lambda: OrderBook()
        self._throttler = throttler or self._get_throttler_instance()

    @classmethod
    def _get_throttler_instance(cls) -> AsyncThrottler:
        throttler = AsyncThrottler(CONSTANTS.RATE_LIMITS)
        return throttler

    @classmethod
    async def get_last_traded_prices(
        cls, trading_pairs: List[str], throttler: Optional[AsyncThrottler] = None
    ) -> Dict[str, float]:
        throttler = throttler or cls._get_throttler_instance()
        tasks = [cls._get_last_traded_price(t_pair, throttler) for t_pair in trading_pairs]
        results = await safe_gather(*tasks)
        return {t_pair: result for t_pair, result in zip(trading_pairs, results)}

    @classmethod
    async def _get_last_traded_price(cls, trading_pair: str, throttler: AsyncThrottler) -> float:
        url = (
            f"{CONSTANTS.BASE_URL}{CONSTANTS.TICKER_PATH_URL}"
            f"?pair={convert_to_exchange_trading_pair(trading_pair)}"
        )
        async with aiohttp.ClientSession() as client:
            async with throttler.execute_task(CONSTANTS.TICKER_PATH_URL):
                resp = await client.get(url)
            resp_json = await resp.json()
            record = list(resp_json["result"].values())[0]
            return float(record["c"][0])

    @classmethod
    async def get_snapshot(
        cls,
        client: aiohttp.ClientSession,
        trading_pair: str,
        limit: int = 1000,
        throttler: Optional[AsyncThrottler] = None,
    ) -> Dict[str, Any]:
        throttler = throttler or cls._get_throttler_instance()
        original_trading_pair: str = trading_pair
        if limit != 0:
            params = {
                "count": str(limit),
                "pair": convert_to_exchange_trading_pair(trading_pair)
            }
        else:
            params = {"pair": convert_to_exchange_trading_pair(trading_pair)}
        async with throttler.execute_task(CONSTANTS.SNAPSHOT_PATH_URL):
            url = f"{CONSTANTS.BASE_URL}{CONSTANTS.SNAPSHOT_PATH_URL}"
            async with client.get(url, params=params) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching Kraken market snapshot for {original_trading_pair}. "
                                  f"HTTP status is {response.status}.")
                response_json = await response.json()
                if len(response_json["error"]) > 0:
                    raise IOError(f"Error fetching Kraken market snapshot for {original_trading_pair}. "
                                  f"Error is {response_json['error']}.")
                data: Dict[str, Any] = next(iter(response_json["result"].values()))
                data = {"trading_pair": trading_pair, **data}
                data["latest_update"] = max([*map(lambda x: x[2], data["bids"] + data["asks"])], default=0.)

                # Need to add the symbol into the snapshot message for the Kafka message queue.
                # Because otherwise, there'd be no way for the receiver to know which market the
                # snapshot belongs to.

                return data

    async def get_new_order_book(self, trading_pair: str) -> OrderBook:
        async with aiohttp.ClientSession() as client:
            snapshot: Dict[str, Any] = await self.get_snapshot(
                client, trading_pair, limit=1000, throttler=self._throttler
            )
            snapshot_timestamp: float = time.time()
            snapshot_msg: OrderBookMessage = KrakenOrderBook.snapshot_message_from_exchange(
                snapshot,
                snapshot_timestamp,
                metadata={"trading_pair": trading_pair}
            )
            order_book: OrderBook = self.order_book_create_function()
            order_book.apply_snapshot(snapshot_msg.bids, snapshot_msg.asks, snapshot_msg.update_id)
            return order_book

    async def _inner_messages(self,
                              ws: websockets.WebSocketClientProtocol) -> AsyncIterable[str]:
        # Terminate the recv() loop as soon as the next message timed out, so the outer loop can reconnect.
        try:
            while True:
                try:
                    msg: str = await asyncio.wait_for(ws.recv(), timeout=self.MESSAGE_TIMEOUT)
                    if ((msg != "{\"event\":\"heartbeat\"}" and
                         "\"event\":\"systemStatus\"" not in msg and
                         "\"event\":\"subscriptionStatus\"" not in msg)):
                        yield msg
                except asyncio.TimeoutError:
                    pong_waiter = await ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=self.PING_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger().warning("WebSocket ping timed out. Going to reconnect...")
            return
        except ConnectionClosed:
            return
        finally:
            await ws.close()

    @classmethod
    async def fetch_trading_pairs(cls, throttler: Optional[AsyncThrottler] = None) -> List[str]:
        throttler = throttler or cls._get_throttler_instance()
        try:
            async with aiohttp.ClientSession() as client:
                async with throttler.execute_task(CONSTANTS.ASSET_PAIRS_PATH_URL):
                    url = f"{CONSTANTS.BASE_URL}{CONSTANTS.ASSET_PAIRS_PATH_URL}"
                    async with client.get(url, timeout=5) as response:
                        if response.status == 200:
                            data: Dict[str, Any] = await response.json()
                            raw_pairs = data.get("result", [])
                            converted_pairs: List[str] = []
                            for pair, details in raw_pairs.items():
                                if "." not in pair:
                                    try:
                                        wsname = details["wsname"]  # pair in format BASE/QUOTE
                                        converted_pairs.append(convert_from_exchange_trading_pair(wsname))
                                    except IOError:
                                        pass
                            return [item for item in converted_pairs]
        except Exception:
            pass
            # Do nothing if the request fails -- there will be no autocomplete for kraken trading pairs
        return []

    async def listen_for_trades(self, ev_loop: asyncio.AbstractEventLoop, output: asyncio.Queue):
        while True:
            try:
                ws_message: str = await self.get_ws_subscription_message("trade")

                async with self._throttler.execute_task(CONSTANTS.WS_CONNECTION_LIMIT_ID):
                    ws = await websockets.connect(CONSTANTS.WS_URL)
                    await ws.send(ws_message)
                    async for raw_msg in self._inner_messages(ws):
                        msg = ujson.loads(raw_msg)
                        trades = [
                            {"pair": convert_from_exchange_trading_pair(msg[-1]), "trade": trade}
                            for trade in msg[1]
                        ]
                        for trade in trades:
                            trade_msg: OrderBookMessage = KrakenOrderBook.trade_message_from_exchange(trade)
                            output.put_nowait(trade_msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error with WebSocket connection. Retrying after 30 seconds...",
                                    exc_info=True)
                await asyncio.sleep(30.0)

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                ws_message: str = await self.get_ws_subscription_message("book")
                async with self._throttler.execute_task(CONSTANTS.WS_CONNECTION_LIMIT_ID):
                    async with websockets.connect(CONSTANTS.WS_URL) as ws:
                        ws: websockets.WebSocketClientProtocol = ws
                        await ws.send(ws_message)
                        async for raw_msg in self._inner_messages(ws):
                            msg = ujson.loads(raw_msg)
                            msg_dict = {"trading_pair": convert_from_exchange_trading_pair(msg[-1]),
                                        "asks": msg[1].get("a", []) or msg[1].get("as", []) or [],
                                        "bids": msg[1].get("b", []) or msg[1].get("bs", []) or []}
                            msg_dict["update_id"] = max(
                                [*map(lambda x: float(x[2]), msg_dict["bids"] + msg_dict["asks"])], default=0.
                            )
                            if "as" in msg[1] and "bs" in msg[1]:
                                order_book_message: OrderBookMessage = (
                                    KrakenOrderBook.snapshot_ws_message_from_exchange(msg_dict, time.time())
                                )
                            else:
                                order_book_message: OrderBookMessage = KrakenOrderBook.diff_message_from_exchange(
                                    msg_dict, time.time())
                            output.put_nowait(order_book_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error with WebSocket connection. Retrying after 30 seconds...",
                                    exc_info=True)
                await asyncio.sleep(30.0)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                async with aiohttp.ClientSession() as client:
                    for trading_pair in self._trading_pairs:
                        try:
                            snapshot: Dict[str, Any] = await self.get_snapshot(
                                client, trading_pair, throttler=self._throttler
                            )
                            snapshot_timestamp: float = time.time()
                            snapshot_msg: OrderBookMessage = KrakenOrderBook.snapshot_message_from_exchange(
                                snapshot,
                                snapshot_timestamp,
                                metadata={"trading_pair": trading_pair}
                            )
                            output.put_nowait(snapshot_msg)
                            self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                            await asyncio.sleep(5.0)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            self.logger().error("Unexpected error. ", exc_info=True)
                            await asyncio.sleep(5.0)
                    this_hour: pd.Timestamp = pd.Timestamp.utcnow().replace(minute=0, second=0, microsecond=0)
                    next_hour: pd.Timestamp = this_hour + pd.Timedelta(hours=1)
                    delta: float = next_hour.timestamp() - time.time()
                    await asyncio.sleep(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error. ", exc_info=True)
                await asyncio.sleep(5.0)

    async def get_ws_subscription_message(self, subscription_type: str):
        # all_markets: pd.DataFrame = await self.get_active_exchange_markets()
        trading_pairs: List[str] = []
        for tp in self._trading_pairs:
            trading_pairs.append(convert_to_exchange_trading_pair(tp, '/'))

        ws_message_dict: Dict[str, Any] = {"event": "subscribe",
                                           "pair": trading_pairs,
                                           "subscription": {"name": subscription_type, "depth": 1000}}

        ws_message: str = ujson.dumps(ws_message_dict)

        return ws_message
