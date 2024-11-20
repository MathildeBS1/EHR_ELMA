"""
Input: Formatted Data
- Load concepts
- Handle wrong data
- Exclude patients with <k concepts
- Split data
- Tokenize
- truncate train and val
"""

import os
from os.path import join

import logging
import dask.dataframe as dd
import torch
from dask.diagnostics import ProgressBar

from corebehrt.classes.excluder import Excluder
from corebehrt.classes.features import FeatureCreator
from corebehrt.classes.tokenizer import EHRTokenizer
from corebehrt.common.config import load_config
from corebehrt.common.setup import DirectoryPreparer, get_args
from corebehrt.functional.split import split_pids_into_pt_ft_test
from corebehrt.classes.loader import FormattedDataLoader
from corebehrt.classes.values import ValueCreator

CONFIG_PATH = "./corebehrt/configs/create_data.yaml"


def main_data(config_path):
    """
    Loads data
    Finds outcomes
    Creates features
    Handles wrong data
    Excludes patients with <k concepts
    Splits data
    Tokenizes
    Saves
    """
    cfg = load_config(config_path)

    DirectoryPreparer(cfg).setup_create_data()

    logger = logging.getLogger("create_data")
    logger.info("Initialize Processors")
    logger.info("Starting feature creation and processing")

    # TODO: temporary fix/check until we split the script into two.
    # As cfg.paths.features is always set, its value cannot be used to decide
    # if features are present.
    if os.path.exists(join(cfg.paths.features, "part.0.parquet")):
        logger.info("Reusing existing features")
    else:
        logger.info("Create and process features")
        create_and_save_features(
            Excluder(**cfg.excluder),  # Excluder is the new Handler and old Excluder
            cfg,
        )
        logger.info("Finished feature creation and processing")
    logger.info(f"Load features from {cfg.paths.features}")
    df = dd.read_parquet(cfg.paths.features, dtype={"concept": "str"})
    pids = df.PID.unique().compute().tolist()
    logger.info("Split into pretrain and finetune.")
    pretrain_pids, finetune_pids, test_pids = split_pids_into_pt_ft_test(
        pids, **cfg.split_ratios
    )
    df_pt = df[df["PID"].isin(pretrain_pids)]
    df_ft_and_test = df[df["PID"].isin(finetune_pids + test_pids)]

    logger.info("Tokenizing")
    vocabulary = None
    if "vocabulary" in cfg.paths:
        logger.info(f"Loading vocabulary from {cfg.paths.vocabulary}")
        vocabulary = torch.load(cfg.paths.vocabulary)
    tokenizer = EHRTokenizer(vocabulary=vocabulary, **cfg.tokenizer)

    # Train tokenizer and tokenzie pt
    df_pt = tokenizer(df_pt)

    # Freeze vocab
    tokenizer.freeze_vocabulary()

    # Tokenize finetune and test and split them
    df_ft_and_test = tokenizer(df_ft_and_test)
    df_ft = df_ft_and_test[df_ft_and_test["PID"].isin(finetune_pids)]
    df_test = df_ft_and_test[df_ft_and_test["PID"].isin(test_pids)]
    logger.info("Save tokenized features")

    # ! We compute each dataframe twice, once to get the pids and once to save the dataframe
    schema = {
        "PID": "str",
        "age": "float32",
        "abspos": "float64",
        "concept": "int32",
        "segment": "int32",
    }
    df_pt.to_parquet(
        join(cfg.paths.tokenized, "features_pretrain"),
        write_index=False,
        schema=schema,
    )
    df_ft.to_parquet(
        join(cfg.paths.tokenized, "features_finetune"),
        write_index=False,
        schema=schema,
    )
    df_test.to_parquet(
        join(cfg.paths.tokenized, "features_test"),
        write_index=False,
        schema=schema,
    )
    torch.save(
        df_pt.compute()["PID"].unique().tolist(),
        join(cfg.paths.tokenized, "pids_pretrain.pt"),
    )
    torch.save(
        df_ft.compute()["PID"].unique().tolist(),
        join(cfg.paths.tokenized, "pids_finetune.pt"),
    )
    torch.save(
        df_test.compute()["PID"].unique().tolist(),
        join(cfg.paths.tokenized, "pids_test.pt"),
    )
    torch.save(tokenizer.vocabulary, join(cfg.paths.tokenized, "vocabulary.pt"))


def create_and_save_features(excluder: Excluder, cfg) -> None:
    """
    Creates features and saves them to disk.
    Returns a list of lists of pids for each batch
    """
    concepts, patients_info = FormattedDataLoader(
        cfg.paths.data,
        cfg.loader.concept_types,
        include_values=(getattr(cfg.loader, "include_values", [])),
    ).load()

    if "values" in cfg.features:
        value_creator = ValueCreator(**cfg.features.values)
        concepts = value_creator(concepts)
        cfg.features.pop("values")

    feature_creator = FeatureCreator(**cfg.features)
    features = feature_creator(patients_info, concepts)

    features = excluder.exclude_incorrect_events(features)
    #! Should we keep this? We're also excluding short sequences in prepare_data
    features = excluder.exclude_short_sequences(features)

    result = features.sort_values(["PID", "abspos"])
    result["concept"] = result["concept"].astype(
        "str"
    )  # we might move this to an earlier stage. E.g. when concatenating concepts
    schema = {
        "PID": "str",
        "age": "float32",
        "abspos": "float64",
        "concept": "str",
        "segment": "int32",
    }
    with ProgressBar():
        result.to_parquet(cfg.paths.features, write_index=False, schema=schema)


if __name__ == "__main__":
    args = get_args(CONFIG_PATH)
    main_data(args.config_path)
