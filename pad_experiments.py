'''
This script runs a baseline model on the given data in train
and/or in test mode, and exports the results.

In order to be used as model inputs, the training data are further split into
sequences using a rolling window of a fixed size.
'''
import argparse
from pathlib import Path
from datetime import datetime

from model.PAD_convLSTM import ConvLSTM
from model.PAD_tempCNN import TempCNN
from model.PAD_convSTAR import ConvSTAR
from model.PAD_unet import UNet
from model.utae import UTAE
from model.SimVP import SimVP

import pytorch_lightning as pl
from pytorch_lightning import loggers as pl_loggers
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint, LearningRateMonitor
from pytorch_lightning.plugins import DDPPlugin
import torch

from utils.PAD_datamodule import PADDataModule
from utils.tools import font_colors
from utils.settings.config import RANDOM_SEED, CROP_ENCODING, LINEAR_ENCODER, CLASS_WEIGHTS, BANDS

# Set seed for everything
pl.seed_everything(RANDOM_SEED)


def resume_or_start(results_path, resume, train, num_epochs, load_checkpoint):
    '''
    Checks whether training must resume or start from scratch and returns
    the appropriate training parameters for each case.

    Parameters
    ----------
    results_path: Path or str
        The path containing the model checkpoints.
    resume: Path or str or None
        Whether to resume training or not.
    train: boolean
        Whether the model must be run in train mode or not.
    num_epochs: int
        The total number of epochs to train for.
    load_checkpoint: Path or str
        The checkpoint to load (if needed).

    Returns
    -------
    (Path, Path, int, int): the path containing results from all runs,
    the path containing the last checkpoint in case of resuming, the last epoch,
    the initial epoch.
    '''
    results_path = Path(results_path)

    if not train:
        # Load the given checkpoint to test with
        load_checkpoint = Path(load_checkpoint)
        run_path = load_checkpoint.parent.parent
        init_epoch = int(load_checkpoint.stem.split('=')[1].split('-')[0])
        max_epoch = init_epoch + 1
        resume_from_checkpoint = load_checkpoint
    elif resume == 'last':
        # Use last run's latest checkpoint to resume training
        run_paths = sorted(results_path.glob('run_*'))
        run_path = run_paths[-1]

        epoch_ckpt = {int(x.stem.split('=')[-1]): x for x in (run_path / 'checkpoints').glob('*')}
        init_epoch = sorted(epoch_ckpt.keys())[-1]
        ckpt_path = epoch_ckpt[init_epoch]

        init_epoch = int(init_epoch)
        max_epoch = init_epoch + num_epochs
        resume_from_checkpoint = ckpt_path
    elif resume is not None:
        # Load the given checkpoint to resume training
        resume = Path(resume)
        run_path = resume.parent.parent
        init_epoch = int(resume.stem.split('=')[1].split('-')[0])
        max_epoch = init_epoch + num_epochs
        resume_from_checkpoint = resume
    elif train:
        # Create folder to save this run's results into
        run_ts = datetime.now().strftime("%Y%m%d%H%M%S")
        run_path = results_path / f'run_{run_ts}'
        run_path.mkdir(exist_ok=True, parents=True)
        resume_from_checkpoint = None
        init_epoch = 0
        max_epoch = num_epochs

    return run_path, resume_from_checkpoint, max_epoch, init_epoch


def create_model_log_path(log_path, prefix, model):
    '''
    Creates the path to contain results for the given model.
    '''
    results_path = log_path / f'{model}' / f'{prefix}'
    results_path.mkdir(exist_ok=True, parents=True)

    return results_path


def main():
    # Parse user arguments
    parser = argparse.ArgumentParser()

    parser.add_argument('--train', action='store_true', default=False, required=False,
                             help='Run in train mode.')
    parser.add_argument('--resume', type=str, default=None, required=False,
                             help='Resume training from the given checkpoint, or the last checkpoint available.')
    parser.add_argument('--devtest', action='store_true', default=False, required=False,
                             help='Perform a dev test run with this model')

    parser.add_argument('--model', type=str, required=True,
                             choices=['convlstm', 'tempcnn', 'convstar', 'unet', 'utae', 'simvp'],
                             help='Model to use. One of [\'convlstm\', \'tempcnn\', \'convstar\', \'unet\', \'simvp\']',
                             )

    parser.add_argument('--parcel_loss', action='store_true', default=False, required=False,
                            help='Use a loss function that takes into account parcel pixels only.')
    parser.add_argument('--weighted_loss', action='store_true', default=False, required=False,
                            help='Use a weighted loss function with precalculated weights per class. Default False.')

    parser.add_argument('--binary_labels', action='store_true', default=False, required=False,
                             help='Map categories to 0 background, 1 parcel. Default False')
    parser.add_argument('--root_dir', type=str, default='dataset',
                        help='The path containing the npy datasets. Default "dataset".')
    parser.add_argument('--work_dir', type=str, help='the dir to save logs and models')

    parser.add_argument('--load_checkpoint', type=str, required=False,
                             help='The checkpoint path to load for model testing.')

    parser.add_argument('--num_epochs', type=int, default=10, required=False,
                             help='Number of epochs. Default 10')
    parser.add_argument('--batch_size', type=int, default=4, required=False,
                             help='The batch size. Default 4')
    parser.add_argument('--lr', type=float, default=1e-1, required=False,
                             help='Starting learning rate. Default 1e-1')

    parser.add_argument('--band_mode', default='nrgb', choices=["nrgb", "rdeg"],
                             help='The image bands to use. Must be space separated')
    parser.add_argument('--img_size', nargs='+', required=False, default=(64,64),
                             help='The size of the subpatch to use as model input. Must be space separated')
    parser.add_argument('--scenario', type=int, choices=[1, 2], default=1,
                             help='scenario') 
    parser.add_argument('--start_month', type=int, default=4, choices=range(1, 12))
    parser.add_argument('--end_month', type=int, default=10, choices=range(1, 14))

    parser.add_argument('--num_workers', type=int, default=6, required=False,
                             help='Number of workers to work on dataloader. Default 6')
    parser.add_argument('--num_gpus', type=int, default=1, required=False,
                             help='Number of gpus to use (per node). Default 1')
    parser.add_argument('--num_nodes', type=int, default=1, required=False,
                             help='Number of nodes to use. Default 1')

    args = parser.parse_args()

    if (not args.train) and (args.load_checkpoint is None):
        print('Error: You should provide the checkpoint to load for model testing!')
        exit(1)

    # Try convert args.img_size to int tuple
    if args.img_size is not None:
        try:
            args.img_size = tuple(map(int, args.img_size))
        except:
            print(f'argument img_size should be castable to int but instead "{args.img_size}" was given!')
            exit(1)

    root_dir = Path(args.root_dir)

    # Create folders for saving and/or retrieving useful files for dataloaders
    log_path = Path('logs')
    log_path.mkdir(exist_ok=True, parents=True)

    loaders_path = log_path / 'loaders'
    loaders_path.mkdir(exist_ok=True, parents=True)

    # Determine prefix
    if not args.work_dir:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        prefix = timestamp
    else:
        prefix = args.work_dir

    # Trainer callbacks
    callbacks = []
    monitor = 'val_loss'

    if args.binary_labels:
        n_classes = 2
    else:
        n_classes = len(list(CROP_ENCODING.values())) + 1

    if args.weighted_loss:
        class_weights = {LINEAR_ENCODER[k]: v for k, v in CLASS_WEIGHTS.items()}
    else:
        class_weights = None

    # define crop_encoding
    crop_encoding_rev = {v: k for k, v in CROP_ENCODING.items()}
    crop_encoding = {k: crop_encoding_rev[k] for k in LINEAR_ENCODER.keys() if k != 0}
    crop_encoding[0] = 'Background/Other'
    timestep = int(args.end_month - args.start_month)
        
    if args.model == 'convlstm':
        args.img_size = [int(dim) for dim in args.img_size]

        results_path = create_model_log_path(log_path, prefix, args.model)

        run_path, resume_from_checkpoint, max_epoch, init_epoch = \
            resume_or_start(results_path, args.resume, args.train, args.num_epochs, args.load_checkpoint)

        if args.train:
            callbacks += [
                LearningRateMonitor(logging_interval='step')
            ]

        if args.resume is not None:
            # Restore optimizer's learning rate
            with open(run_path / 'lrs.txt', 'r') as f:
                for line in f:
                    epoch_lr = line.strip().split(': ')
                    if int(epoch_lr[0]) == init_epoch:
                        init_learning_rate = float(epoch_lr[1])

            model = ConvLSTM(run_path, LINEAR_ENCODER, learning_rate=init_learning_rate,
                             parcel_loss=args.parcel_loss, class_weights=class_weights)
        else:
            model = ConvLSTM(run_path, LINEAR_ENCODER, parcel_loss=args.parcel_loss,
                             class_weights=class_weights)

        if not args.train:
            # Load the model for testing
            model = ConvLSTM.load_from_checkpoint(resume_from_checkpoint,
                                                  map_location=torch.device('cpu'),
                                                  run_path=run_path,
                                                  linear_encoder=LINEAR_ENCODER,
                                                  crop_encoding=crop_encoding,
                                                  checkpoint_epoch=init_epoch)
    elif args.model == 'convstar':
        args.img_size = [int(dim) for dim in args.img_size]

        results_path = create_model_log_path(log_path, prefix, args.model)

        run_path, resume_from_checkpoint, max_epoch, init_epoch = \
            resume_or_start(results_path, args.resume, args.train, args.num_epochs, args.load_checkpoint)

        if args.train:
            callbacks += [
                LearningRateMonitor(logging_interval='step')
            ]

        if args.resume is not None:
            # Restore optimizer's learning rate
            with open(run_path / 'lrs.txt', 'r') as f:
                for line in f:
                    epoch_lr = line.strip().split(': ')
                    if int(epoch_lr[0]) == init_epoch:
                        init_learning_rate = float(epoch_lr[1])

            model = ConvSTAR(run_path, LINEAR_ENCODER, learning_rate=init_learning_rate,
                             parcel_loss=args.parcel_loss, class_weights=class_weights)
        else:
            model = ConvSTAR(run_path, LINEAR_ENCODER, parcel_loss=args.parcel_loss,
                             class_weights=class_weights)

        if not args.train:
            model = ConvSTAR.load_from_checkpoint(resume_from_checkpoint,
                                                  map_location=torch.device('cpu'),
                                                  run_path=run_path,
                                                  linear_encoder=LINEAR_ENCODER,
                                                  crop_encoding=crop_encoding,
                                                  checkpoint_epoch=init_epoch)
    elif args.model == 'unet':
        args.img_size = [int(dim) for dim in args.img_size]

        results_path = create_model_log_path(log_path, prefix, args.model)

        run_path, resume_from_checkpoint, max_epoch, init_epoch = \
            resume_or_start(results_path, args.resume, args.train, args.num_epochs, args.load_checkpoint)

        if args.train:
            callbacks += [
                LearningRateMonitor(logging_interval='step')
            ]

        if args.resume is not None:
            # Restore optimizer's learning rate
            with open(run_path / 'lrs.txt', 'r') as f:
                for line in f:
                    epoch_lr = line.strip().split(': ')
                    if int(epoch_lr[0]) == init_epoch:
                        init_learning_rate = float(epoch_lr[1])

            model = UNet(run_path, LINEAR_ENCODER, learning_rate=init_learning_rate,
                         parcel_loss=args.parcel_loss, class_weights=class_weights,
                         num_layers=3)
        else:
            model = UNet(run_path, LINEAR_ENCODER, parcel_loss=args.parcel_loss,
                         class_weights=class_weights, num_layers=3)

        if not args.train:
            model = UNet.load_from_checkpoint(
                resume_from_checkpoint,
                map_location=torch.device('cpu'),
                run_path=run_path,
                linear_encoder=LINEAR_ENCODER,
                crop_encoding=crop_encoding,
                checkpoint_epoch=init_epoch,
                num_layer=3)

    elif args.model == 'tempcnn':
        args.img_size = (1, 1)
        args.bands = ['B03', 'B04', 'B08']

        results_path = create_model_log_path(log_path, prefix, args.model)

        run_path, resume_from_checkpoint, max_epoch, init_epoch = \
            resume_or_start(results_path, args.resume, args.train, args.num_epochs, args.load_checkpoint)

        model = TempCNN(3, n_classes, args.window_len, run_path, LINEAR_ENCODER,
                        kernel_size=3, parcel_loss=args.parcel_loss, class_weights=class_weights)

        if not args.train:
            model = TempCNN.load_from_checkpoint(args.load_checkpoint,
                                                 map_location=torch.device('cpu'),
                                                 input_dim=3,
                                                 nclasses=n_classes,
                                                 sequence_length=args.window_len,
                                                 run_path=run_path,
                                                 linear_encoder=LINEAR_ENCODER,
                                                 crop_encoding=crop_encoding)

    elif args.model == 'utae':
        results_path = create_model_log_path(log_path, prefix, args.model)
        run_path, resume_from_checkpoint, max_epoch, init_epoch = \
            resume_or_start(results_path, args.resume, args.train, args.num_epochs, args.load_checkpoint)
        
        if args.resume is not None:
            # Restore optimizer's learning rate
            with open(run_path / 'lrs.txt', 'r') as f:
                for line in f:
                    epoch_lr = line.strip().split(': ')
                    if int(epoch_lr[0]) == init_epoch:
                        init_learning_rate = float(epoch_lr[1])

            model = UTAE(run_path, 
                        LINEAR_ENCODER,
                        learning_rate=init_learning_rate,
                        parcel_loss=args.parcel_loss,
                        class_weights=class_weights,
                        input_size=4
                        )
        else:
            model = UTAE(run_path, 
                        LINEAR_ENCODER,
                        parcel_loss=args.parcel_loss,
                        class_weights=class_weights,
                        input_size=4
                        )

        if not args.train:
            model = UTAE.load_from_checkpoint(
                resume_from_checkpoint,
                map_location=torch.device('cpu'),
                run_path=run_path,
                linear_encoder=LINEAR_ENCODER,
                crop_encoding=crop_encoding,
                checkpoint_epoch=init_epoch,
                input_size=4)

    elif args.model == 'simvp':
        results_path = create_model_log_path(log_path, prefix, args.model)
        run_path, resume_from_checkpoint, max_epoch, init_epoch = \
            resume_or_start(results_path, args.resume, args.train, args.num_epochs, args.load_checkpoint)

        model = SimVP(run_path, 
                      LINEAR_ENCODER,
                      parcel_loss=args.parcel_loss,
                      class_weights=class_weights,
                      crop_encoding=crop_encoding,
                      shape_in=[timestep,4,64,64],
                      hid_S=64,
                      hid_T=512,
                      N_S=4,
                      N_T=8,
                      incep_ker=[3,5,7,11], 
                      groups=8, 
                      learning_rate=0.001)
        model.load_state_dict(torch.load('./checkpoint.pth'), strict=False)

        if not args.train:
            model = SimVP.load_from_checkpoint(
                resume_from_checkpoint,
                map_location=torch.device('cpu'),
                run_path=run_path,
                linear_encoder=LINEAR_ENCODER,
                crop_encoding=crop_encoding,
                class_weights=class_weights,
                shape_in=[timestep,4,64,64],
                hid_S=64,
                hid_T=512,
                N_S=4,
                N_T=8,
                incep_ker=[3,5,7,11], 
                groups=8)

    # Create Data Modules
    dm = PADDataModule(
        root_dir=args.root_dir,
        scenario=args.scenario,
        band_mode=args.band_mode,
        linear_encoder=LINEAR_ENCODER,
        start_month=args.start_month,
        end_month=args.end_month,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        binary_labels=args.binary_labels,
        return_parcels=args.parcel_loss
    )

    if args.train:
        dm.setup('fit')
        # Early stopping
        # early_stopping = EarlyStopping('val_loss')
        callbacks.append(
            ModelCheckpoint(
                dirpath=run_path / 'checkpoints',
                monitor=monitor,
                mode='min',
                save_top_k=-1
            )
        )

        tb_logger = pl_loggers.TensorBoardLogger(run_path / 'tensorboard')
        my_ddp = DDPPlugin(find_unused_parameters=True)
        trainer = pl.Trainer(gpus=args.num_gpus,
                             num_nodes=args.num_nodes,
                             progress_bar_refresh_rate=20,
                             min_epochs=1,
                             max_epochs=max_epoch + 1,
                             check_val_every_n_epoch=1,
                             precision=32,
                             callbacks=callbacks,
                             logger=tb_logger,
                             gradient_clip_val=10.0,
                             # early_stop_callback=early_stopping,
                             checkpoint_callback=True,
                             resume_from_checkpoint=resume_from_checkpoint,
                             fast_dev_run=args.devtest,
                             strategy='ddp' if args.num_gpus > 1 else None,
                             plugins=[my_ddp]
                             )
        trainer.fit(model, datamodule=dm)

        # Setup to multi-GPUs
        dm.setup('test')
        my_ddp = DDPPlugin(find_unused_parameters=True)
        trainer = pl.Trainer(gpus=args.num_gpus,
                             num_nodes=args.num_nodes,
                             progress_bar_refresh_rate=1,
                             min_epochs=1,
                             max_epochs=2,
                             precision=32,
                             strategy='ddp' if args.num_gpus > 1 else None,
                             plugins=[my_ddp]
                             )
        # Test model
        model.eval()
        trainer.test(model, datamodule=dm)

    else:
        # Setup to multi-GPUs
        dm.setup('test')
        my_ddp = DDPPlugin(find_unused_parameters=True)
        trainer = pl.Trainer(gpus=args.num_gpus,
                             num_nodes=args.num_nodes,
                             progress_bar_refresh_rate=1,
                             min_epochs=1,
                             max_epochs=2,
                             precision=32,
                             strategy='ddp' if args.num_gpus > 1 else None,
                             plugins=[my_ddp]
                             )
        # Test model
        model.eval()
        trainer.test(model, datamodule=dm)


if __name__ == '__main__':
    main()
