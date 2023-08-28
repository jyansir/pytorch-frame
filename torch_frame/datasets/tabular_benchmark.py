import os

import pandas as pd

import torch_frame


class TabularBenchmark(torch_frame.data.Dataset):
    r"""A collection of Tabular benchmark datasets introduced in
    https://arxiv.org/abs/2207.08815."""

    name_to_task_category = {
        'albert': 'clf_cat',
        'compas-two-years': 'clf_cat',
        'covertype': 'clf_cat',
        'default-of-credit-card-clients': 'clf_cat',
        'electricity': 'clf_cat',
        'eye_movements': 'clf_cat',
        'road-safety': 'clf_cat',
        'Bioresponse': 'clf_num',
        'Diabetes130US': 'clf_num',
        'Higgs': 'clf_num',
        'MagicTelescope': 'clf_num',
        'MiniBooNE': 'clf_num',
        'bank-marketing': 'clf_num',
        'california': 'clf_num',
        'credit': 'clf_num',
        'heloc': 'clf_num',
        'house_16H': 'clf_num',
        'jannis': 'clf_num',
        'pol': 'clf_num',
    }

    large_datasets = {
        'covertype',
        'road-safety',
        'Higgs',
        'MiniBooNE',
        'jannis',
    }

    base_url = 'https://huggingface.co/datasets/inria-soda/tabular-benchmark/raw/main/'  # noqa
    # Dedicated URLs for large datasets
    base_url_large = 'https://huggingface.co/datasets/inria-soda/tabular-benchmark/resolve/main/'  # noqa

    # TODO: Add regression datasets
    # https://huggingface.co/datasets/inria-soda/tabular-benchmark/tree/main/reg_cat
    # https://huggingface.co/datasets/inria-soda/tabular-benchmark/tree/main/reg_num

    def __init__(self, root: str, name: str):
        if name not in self.name_to_task_category:
            raise ValueError(
                f"The given dataset name ('{name}') is not available. It "
                f"needs to be chosen from "
                f"{list(self.name_to_task_category.keys())}.")
        base_url = (self.base_url_large
                    if name in self.large_datasets else self.base_url)
        url = os.path.join(
            base_url,
            self.name_to_task_category[name],
            f'{name}.csv',
        )
        path = self.download_url(url, root)
        df = pd.read_csv(path)
        # The last column is the target column
        target_col = df.columns[-1]
        col_to_stype = {}
        for col in df.columns:
            if df[col].dtype == float:
                col_to_stype[col] = torch_frame.numerical
            else:
                col_to_stype[col] = torch_frame.categorical
        super().__init__(df, col_to_stype, target_col=target_col)
