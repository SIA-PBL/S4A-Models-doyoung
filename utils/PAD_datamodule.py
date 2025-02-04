import numpy as np
import time
import torch
from typing import Any, Union
from pathlib import Path
from pycocotools.coco import COCO
from torch.utils.data import DataLoader
import pytorch_lightning as pl

from .settings.config import RANDOM_SEED, IMG_SIZE
from .PAD_dataset import PADDataset
from .npy_dataset import NpyPADDataset

# Set seed for everything
pl.seed_everything(RANDOM_SEED)


class PADDataModule(pl.LightningDataModule):
    # Documentation: https://pytorch-lightning.readthedocs.io/en/latest/extensions/datamodules.html
    '''
    PyTorch Lightning DataModule Wrapper for PADDataset
    '''

    def __init__(
            self,
            root_dir: Union[str, Path] = Path(),
            scenario: int = 1,
            band_mode: str = 'nrgb',
            linear_encoder: dict = None,
            start_month: int = 4,
            end_month: int = 10,
            batch_size: int = 64,
            num_workers: int = 4,
            binary_labels: bool = False,
            return_parcels: bool = False
    ) -> None:
        '''
        Parameters
        ----------
        bands: list of str, default None
            A list of the bands to use. If None, then all available bands are
            taken into consideration. Note that the bands are given in a two-digit
            format, e.g. '01', '02', '8A', etc.
        linear_encoder: dict, default None
            Maps arbitrary crop_ids to range 0-len(unique(crop_id)).
        output_size: tuple of int, default None
            If a tuple (H, W) is given, then the output images will be divided
            into non-overlapping subpatches of size (H, W). Otherwise, the images
            will retain their original size.
        batch_size: int, default 64
            The batch size to use.
        num_workers: int, default 4
            The number of workers to use.
        binary_labels: bool, default False
            Map categories to 0 background, 1 parcel.
        return_parcels: bool, default False
            If True, then a boolean mask for the parcels is also returned.
        '''

        super().__init__()

        self.root_dir = root_dir
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.binary_labels = binary_labels

        # Initialize parameters required for Patches Dataset
        self.band_mode = band_mode
        self.linear_encoder = linear_encoder
        self.return_parcels = return_parcels

        self.start_month = start_month
        self.end_month = end_month
        self.scenario = scenario

        self.img_size = IMG_SIZE

    def setup(self, stage=None):
        # called on every GPU
        # Create train/val/test loaders
        assert stage in ['fit', 'test'], f'Stage : "{stage}" must be fit or test!'

        if stage == 'fit':
            self.dataset_train = NpyPADDataset(root_dir=self.root_dir,
                                               band_mode=self.band_mode,
                                               start_month=self.start_month,
                                               end_month=self.end_month,
                                               mode='train',
                                               return_parcels=self.return_parcels,
                                               scenario=self.scenario,
                                               )
            self.dataset_eval = NpyPADDataset(root_dir=self.root_dir,
                                              band_mode=self.band_mode,
                                              start_month=self.start_month,
                                              end_month=self.end_month,
                                              mode='val',
                                              return_parcels=self.return_parcels,
                                              scenario=self.scenario,
                                              )

        else:
            self.dataset_test = NpyPADDataset(root_dir=self.root_dir,
                                              band_mode=self.band_mode,
                                              start_month=self.start_month,
                                              end_month=self.end_month,
                                              output_size=None, # (H, W) = (366, 366)
                                              mode='test',
                                              return_parcels=self.return_parcels,
                                              scenario=self.scenario
                                              )

    def train_dataloader(self):
        return DataLoader(
            self.dataset_train,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=self.num_workers,
            pin_memory=True
        )

    def val_dataloader(self):
        return DataLoader(
            self.dataset_eval,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            pin_memory=True
        )

    def test_dataloader(self):
        return DataLoader(
            self.dataset_test,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            pin_memory=True
        )
