# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import os
from pathlib import Path

import pandas as pd
from torch.utils.data import Dataset


def _get_orders(order_dir: Path) -> pd.DataFrame:
    if os.path.isfile(order_dir):
        return pd.read_pickle(order_dir)
    else:
        orders = []
        for file in order_dir.iterdir():
            orders.append(pd.read_pickle(file))
        return pd.concat(orders)


class QlibSingleAssetOrderExecutionDataset(Dataset):
    def __init__(
        self,
        order_dir: Path,
        subset: str = None,
    ) -> None:
        self._orders = _get_orders(order_dir)

    def __len__(self):
        return len(self._orders)

    def __getitem__(self, idx):
        order = self._orders.iloc[idx]
        # TODO: to be finished
