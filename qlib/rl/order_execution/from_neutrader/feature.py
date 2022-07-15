import collections
import pickle
from typing import List, Optional

import pandas as pd

import qlib
from qlib.config import QlibConfig, REG_CN
from qlib.contrib.ops.high_freq import BFillNan, Cut, Date, DayCumsum, DayLast, FFillNan, IsInf, IsNull, Select
from qlib.data.dataset import DatasetH

_dataset = None


class LRUCache:
    def __init__(self, pool_size: int = 200):
        self.pool_size = pool_size
        self.contents = dict()
        self.keys = collections.deque()

    def put(self, key, item):
        if self.has(key):
            self.keys.remove(key)
        self.keys.append(key)
        self.contents[key] = item
        while len(self.contents) > self.pool_size:
            self.contents.pop(self.keys.popleft())

    def get(self, key):
        return self.contents[key]

    def has(self, key):
        return key in self.contents


class DataWrapper:
    def __init__(
        self,
        feature_dataset: DatasetH,
        backtest_dataset: DatasetH,
        columns_today: List[str],
        columns_yesterday: List[str],
        _internal: bool = False,
    ):
        assert _internal, "Init function of data wrapper is for internal use only."

        self.feature_dataset = feature_dataset
        self.backtest_dataset = backtest_dataset
        self.columns_today = columns_today
        self.columns_yesterday = columns_yesterday

        self.feature_cache = LRUCache()
        self.backtest_cache = LRUCache()

    def get(self, stock_id: str, date: pd.Timestamp, backtest: bool = False):
        start_time, end_time = date.replace(hour=0, minute=0, second=0), date.replace(hour=23, minute=59, second=59)

        dataset = self.backtest_dataset if backtest else self.feature_dataset

        if backtest:
            dataset = self.backtest_dataset
            cache = self.backtest_cache
        else:
            dataset = self.feature_dataset
            cache = self.feature_cache

        if cache.has((start_time, end_time, stock_id)):
            return cache.get((start_time, end_time, stock_id))
        data = dataset.handler.fetch(pd.IndexSlice[stock_id, start_time:end_time], level=None)
        cache.put((start_time, end_time, stock_id), data)
        return data


def init_qlib(config: QlibConfig, part: Optional[str] = None) -> None:
    global _dataset

    provider_uri_map = {
        "day": config.provider_uri_day.as_posix(),
        "1min": config.provider_uri_1min.as_posix(),
    }
    qlib.init(
        region=REG_CN,
        auto_mount=False,
        custom_ops=[DayLast, FFillNan, BFillNan, Date, Select, IsNull, IsInf, Cut, DayCumsum],
        expression_cache=None,
        calendar_provider={
            "class": "LocalCalendarProvider",
            "module_path": "qlib.data.data",
            "kwargs": {
                "backend": {
                    "class": "FileCalendarStorage",
                    "module_path": "qlib.data.storage.file_storage",
                    "kwargs": {"provider_uri_map": provider_uri_map},
                }
            },
        },
        feature_provider={
            "class": "LocalFeatureProvider",
            "module_path": "qlib.data.data",
            "kwargs": {
                "backend": {
                    "class": "FileFeatureStorage",
                    "module_path": "qlib.data.storage.file_storage",
                    "kwargs": {"provider_uri_map": provider_uri_map},
                }
            },
        },
        provider_uri=provider_uri_map,
        kernels=1,
        redis_port=-1,
        clear_mem_cache=False,  # init_qlib will be called for multiple times. Keep the cache for improving performance
    )

    # this won't work if it's put outside in case of multiprocessing

    if part is None:
        feature_path = config.feature_root_dir / "feature.pkl"
        backtest_path = config.feature_root_dir / "backtest.pkl"
    else:
        feature_path = config.feature_root_dir / "feature" / (part + ".pkl")
        backtest_path = config.feature_root_dir / "backtest" / (part + ".pkl")

    with feature_path.open("rb") as f:
        print(feature_path)
        feature_dataset = pickle.load(f)
    with backtest_path.open("rb") as f:
        backtest_dataset = pickle.load(f)

    _dataset = DataWrapper(
        feature_dataset,
        backtest_dataset,
        config.feature_columns_today,
        config.feature_columns_yesterday,
        _internal=True,
    )