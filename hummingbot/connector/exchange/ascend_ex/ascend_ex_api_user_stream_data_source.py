#!/usr/bin/env python
import time
import asyncio
import logging
import websockets
import aiohttp
import ujson

from typing import Optional, List, AsyncIterable, Any

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.logger import HummingbotLogger
from hummingbot.connector.exchange.ascend_ex.ascend_ex_auth import AscendExAuth
from hummingbot.connector.exchange.ascend_ex import ascend_ex_constants as CONSTANTS
from hummingbot.connector.exchange.ascend_ex.ascend_ex_utils import get_ws_url_private


class AscendExAPIUserStreamDataSource(UserStreamTrackerDataSource):
    MAX_RETRIES = 20
    MESSAGE_TIMEOUT = 10.0
    PING_TIMEOUT = 5.0

    _logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    def __init__(
        self, throttler: AsyncThrottler, ascend_ex_auth: AscendExAuth, trading_pairs: Optional[List[str]] = None
    ):
        super().__init__()
        self._throttler = throttler
        self._ascend_ex_auth: AscendExAuth = ascend_ex_auth
        self._trading_pairs = trading_pairs or []
        self._current_listen_key = None
        self._listen_for_user_stream_task = None
        self._last_recv_time: float = 0

    @property
    def last_recv_time(self) -> float:
        return self._last_recv_time

    async def listen_for_user_stream(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue) -> AsyncIterable[Any]:
        """
        *required
        Subscribe to user stream via web socket, and keep the connection open for incoming messages
        :param ev_loop: ev_loop to execute this function in
        :param output: an async queue where the incoming messages are stored
        """

        while True:
            try:
                headers = {
                    **self._ascend_ex_auth.get_headers(),
                    **self._ascend_ex_auth.get_auth_headers("info"),
                }
                async with self._throttler.execute_task(CONSTANTS.INFO_PATH_URL):
                    response = await aiohttp.ClientSession().get(
                        f"{CONSTANTS.REST_URL}/{CONSTANTS.INFO_PATH_URL}", headers=headers
                    )
                info = await response.json()
                accountGroup = info.get("data").get("accountGroup")
                headers = self._ascend_ex_auth.get_auth_headers("stream")
                payload = {
                    "op": CONSTANTS.SUB_ENDPOINT_NAME,
                    "ch": "order:cash"
                }

                async with websockets.connect(f"{get_ws_url_private(accountGroup)}/stream", extra_headers=headers) as ws:
                    try:
                        ws: websockets.WebSocketClientProtocol = ws
                        async with self._throttler.execute_task(CONSTANTS.SUB_ENDPOINT_NAME):
                            await ws.send(ujson.dumps(payload))

                        async for raw_msg in self._inner_messages(ws):
                            try:
                                msg = ujson.loads(raw_msg)
                                if msg is None:
                                    continue

                                output.put_nowait(msg)
                            except Exception:
                                self.logger().error(
                                    "Unexpected error when parsing AscendEx message. ", exc_info=True
                                )
                                raise
                    except Exception:
                        self.logger().error(
                            "Unexpected error while listening to AscendEx messages. ", exc_info=True
                        )
                        raise
                    finally:
                        await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(
                    "Unexpected error with AscendEx WebSocket connection. " "Retrying after 30 seconds...", exc_info=True
                )
                await asyncio.sleep(30.0)

    async def _inner_messages(
        self,
        ws: websockets.WebSocketClientProtocol
    ) -> AsyncIterable[str]:
        # Terminate the recv() loop as soon as the next message timed out, so the outer loop can reconnect.
        try:
            while True:
                try:
                    raw_msg: str = await asyncio.wait_for(ws.recv(), timeout=self.MESSAGE_TIMEOUT)
                    self._last_recv_time = time.time()
                    yield raw_msg
                except asyncio.TimeoutError:
                    payload = {"op": CONSTANTS.PONG_ENDPOINT_NAME}
                    pong_waiter = ws.send(ujson.dumps(payload))
                    async with self._throttler.execute_task(CONSTANTS.PONG_ENDPOINT_NAME):
                        await asyncio.wait_for(pong_waiter, timeout=self.PING_TIMEOUT)
                    self._last_recv_time = time.time()
        except asyncio.TimeoutError:
            self.logger().warning("WebSocket ping timed out. Going to reconnect...")
            return
        except websockets.ConnectionClosed:
            return
        finally:
            await ws.close()
