# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from __future__ import annotations

from typing import NamedTuple, Optional, cast

import numpy as np
import pandas as pd
from typing_extensions import TypedDict

from qlib.backtest import Order
from qlib.backtest.executor import BaseExecutor
from qlib.constant import EPS
from qlib.rl.data.exchange_wrapper import QlibIntradayBacktestData
from qlib.rl.data.pickle_styled import IntradayBacktestData
from qlib.rl.order_execution.utils import dataframe_append, get_simulator_executor, price_advantage


class SAOEStateMaintainer:
    """
    Maintain states of the environment.

    Example usage::

        maintainer = StateMaintainer(...)
        maintainer.update(...)
        state = maintainer.saoe_state
    """

    def __init__(
        self,
        order: Order,
        executor: BaseExecutor,
        backtest_data: QlibIntradayBacktestData,
        time_per_step: str,
        ticks_index: pd.DatetimeIndex,
        twap_price: float,
        ticks_for_order: pd.DatetimeIndex,
    ) -> None:
        super().__init__()

        self.position = order.amount
        self.order = order
        self.executor = executor
        self.backtest_data = backtest_data
        self.time_per_step = time_per_step
        self.ticks_index = ticks_index
        self.ticks_for_order = ticks_for_order
        self.twap_price = twap_price

        metric_keys = list(SAOEMetrics.__annotations__.keys())  # pylint: disable=no-member
        self.history_exec = pd.DataFrame(columns=metric_keys).set_index("datetime")
        self.history_steps = pd.DataFrame(columns=metric_keys).set_index("datetime")
        self.metrics: Optional[SAOEMetrics] = None

        self.cur_time = ticks_for_order[0]
        self.ticks_per_step = int(pd.Timedelta(self.time_per_step).total_seconds() // 60)

    def _next_time(self) -> pd.Timestamp:
        current_loc = self.ticks_index.get_loc(self.cur_time)
        next_loc = current_loc + self.ticks_per_step
        next_loc = next_loc - next_loc % self.ticks_per_step
        if next_loc < len(self.ticks_index) and self.ticks_index[next_loc] < self.order.end_time:
            return self.ticks_index[next_loc]
        else:
            return self.order.end_time

    def update(self, execute_result: list) -> None:
        exec_vol = np.array([e[0].deal_amount for e in execute_result])
        num_step = len(execute_result)

        if num_step == 0:
            market_volume = np.array([])
            market_price = np.array([])
            datetime_list = pd.DatetimeIndex([])
        else:
            market_volume = np.array(
                self.executor.trade_exchange.get_volume(
                    self.order.stock_id,
                    execute_result[0][0].start_time,
                    execute_result[-1][0].start_time,
                    method=None,
                ),
            )

            # Get data from the SimulatorExecutor's (lowest-level executor) indicator
            simulator_executor = get_simulator_executor(self.executor)
            simulator_trade_account = simulator_executor.trade_account
            simulator_df = simulator_trade_account.get_trade_indicator().generate_trade_indicators_dataframe()

            trade_value = simulator_df.iloc[-num_step:]["value"].values
            deal_amount = simulator_df.iloc[-num_step:]["deal_amount"].values
            market_price = trade_value / deal_amount
            datetime_list = simulator_df.index[-num_step:]

        assert market_price.shape == market_volume.shape == exec_vol.shape

        # Get data from the current level executor's indicator
        current_trade_account = self.executor.trade_account
        current_df = current_trade_account.get_trade_indicator().generate_trade_indicators_dataframe()
        self.history_exec = dataframe_append(
            self.history_exec,
            self._collect_multi_order_metric(
                order=self.order,
                datetime=datetime_list,
                market_vol=market_volume,
                market_price=market_price,
                exec_vol=exec_vol,
                pa=current_df.iloc[-1]["pa"],
            ),
        )

        self.history_steps = dataframe_append(
            self.history_steps,
            [
                self._collect_single_order_metric(
                    self.order,
                    self.cur_time,
                    market_volume,
                    market_price,
                    exec_vol.sum(),
                    exec_vol,
                ),
            ],
        )

        # TODO: check whether we need this. Can we get this information from Account?
        # Do this at the end
        self.position -= exec_vol.sum()

        self.cur_time = self._next_time()

    def generate_metrics_after_done(self) -> None:
        """Generate metrics once the upper level execution is done"""

        self.metrics = self._collect_single_order_metric(
            self.order,
            self.ticks_index[0],  # start time
            self.history_exec["market_volume"],
            self.history_exec["market_price"],
            self.history_steps["amount"].sum(),
            self.history_exec["deal_amount"],
        )

    def _collect_multi_order_metric(
        self,
        order: Order,
        datetime: pd.Timestamp,
        market_vol: np.ndarray,
        market_price: np.ndarray,
        exec_vol: np.ndarray,
        pa: float,
    ) -> SAOEMetrics:
        return SAOEMetrics(
            # It should have the same keys with SAOEMetrics,
            # but the values do not necessarily have the annotated type.
            # Some values could be vectorized (e.g., exec_vol).
            stock_id=order.stock_id,
            datetime=datetime,
            direction=order.direction,
            market_volume=market_vol,
            market_price=market_price,
            amount=exec_vol,
            inner_amount=exec_vol,
            deal_amount=exec_vol,
            trade_price=market_price,
            trade_value=market_price * exec_vol,
            position=self.position - np.cumsum(exec_vol),
            ffr=exec_vol / order.amount,
            pa=pa,
        )

    def _collect_single_order_metric(
        self,
        order: Order,
        datetime: pd.Timestamp,
        market_vol: np.ndarray,
        market_price: np.ndarray,
        amount: float,  # intended to trade such amount
        exec_vol: np.ndarray,
    ) -> SAOEMetrics:
        assert len(market_vol) == len(market_price) == len(exec_vol)

        if np.abs(np.sum(exec_vol)) < EPS:
            exec_avg_price = 0.0
        else:
            exec_avg_price = cast(float, np.average(market_price, weights=exec_vol))  # could be nan
            if hasattr(exec_avg_price, "item"):  # could be numpy scalar
                exec_avg_price = exec_avg_price.item()  # type: ignore

        exec_sum = exec_vol.sum()
        return SAOEMetrics(
            stock_id=order.stock_id,
            datetime=datetime,
            direction=order.direction,
            market_volume=market_vol.sum(),
            market_price=market_price.mean() if len(market_price) > 0 else np.nan,
            amount=amount,
            inner_amount=exec_sum,
            deal_amount=exec_sum,  # in this simulator, there's no other restrictions
            trade_price=exec_avg_price,
            trade_value=float(np.sum(market_price * exec_vol)),
            position=self.position - exec_sum,
            ffr=float(exec_sum / order.amount),
            pa=price_advantage(exec_avg_price, self.twap_price, order.direction),
        )

    @property
    def saoe_state(self) -> SAOEState:
        return SAOEState(
            order=self.order,
            cur_time=self.executor.trade_calendar.get_step_time()[0],
            position=self.position,
            history_exec=self.history_exec,
            history_steps=self.history_steps,
            metrics=self.metrics,
            backtest_data=self.backtest_data,
            ticks_per_step=int(pd.Timedelta(self.time_per_step).total_seconds() // 60),
            ticks_index=self.ticks_index,
            ticks_for_order=self.ticks_for_order,
        )


class SAOEMetrics(TypedDict):
    """Metrics for SAOE accumulated for a "period".
    It could be accumulated for a day, or a period of time (e.g., 30min), or calculated separately for every minute.

    Warnings
    --------
    The type hints are for single elements. In lots of times, they can be vectorized.
    For example, ``market_volume`` could be a list of float (or ndarray) rather tahn a single float.
    """

    stock_id: str
    """Stock ID of this record."""
    datetime: pd.Timestamp | pd.DatetimeIndex  # TODO: check this
    """Datetime of this record (this is index in the dataframe)."""
    direction: int
    """Direction of the order. 0 for sell, 1 for buy."""

    # Market information.
    market_volume: np.ndarray | float
    """(total) market volume traded in the period."""
    market_price: np.ndarray | float
    """Deal price. If it's a period of time, this is the average market deal price."""

    # Strategy records.

    amount: np.ndarray | float
    """Total amount (volume) strategy intends to trade."""
    inner_amount: np.ndarray | float
    """Total amount that the lower-level strategy intends to trade
    (might be larger than amount, e.g., to ensure ffr)."""

    deal_amount: np.ndarray | float
    """Amount that successfully takes effect (must be less than inner_amount)."""
    trade_price: np.ndarray | float
    """The average deal price for this strategy."""
    trade_value: np.ndarray | float
    """Total worth of trading. In the simple simulation, trade_value = deal_amount * price."""
    position: np.ndarray | float
    """Position left after this "period"."""

    # Accumulated metrics

    ffr: np.ndarray | float
    """Completed how much percent of the daily order."""

    pa: np.ndarray | float
    """Price advantage compared to baseline (i.e., trade with baseline market price).
    The baseline is trade price when using TWAP strategy to execute this order.
    Please note that there could be data leak here).
    Unit is BP (basis point, 1/10000)."""


class SAOEState(NamedTuple):
    """Data structure holding a state for SAOE simulator."""

    order: Order
    """The order we are dealing with."""
    cur_time: pd.Timestamp
    """Current time, e.g., 9:30."""
    position: float
    """Current remaining volume to execute."""
    history_exec: pd.DataFrame
    """See :attr:`SingleAssetOrderExecution.history_exec`."""
    history_steps: pd.DataFrame
    """See :attr:`SingleAssetOrderExecution.history_steps`."""

    metrics: Optional[SAOEMetrics]
    """Daily metric, only available when the trading is in "done" state."""

    backtest_data: IntradayBacktestData
    """Backtest data is included in the state.
    Actually, only the time index of this data is needed, at this moment.
    I include the full data so that algorithms (e.g., VWAP) that relies on the raw data can be implemented.
    Interpreter can use this as they wish, but they should be careful not to leak future data.
    """

    ticks_per_step: int
    """How many ticks for each step."""
    ticks_index: pd.DatetimeIndex
    """Trading ticks in all day, NOT sliced by order (defined in data). e.g., [9:30, 9:31, ..., 14:59]."""
    ticks_for_order: pd.DatetimeIndex
    """Trading ticks sliced by order, e.g., [9:45, 9:46, ..., 14:44]."""