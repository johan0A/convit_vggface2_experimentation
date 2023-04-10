import os
import torch
from PIL import Image
from torchvision.datasets import DatasetFolder
from torchvision.transforms import ToTensor

import torch
from torch.utils.data import DataLoader

import torch.nn as nn

from timm.data import create_transform

import sys

# imports from main file from the convit repo:
import argparse
import datetime
import numpy as np
import time
import torch
import torch.backends.cudnn as cudnn
import json
import gc

from pathlib import Path

from timm.data import Mixup
from timm.models import create_model
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from timm.scheduler import create_scheduler
from timm.optim import create_optimizer
from timm.utils import NativeScaler, get_state_dict, ModelEma

from datasets import build_dataset
from engine import train_one_epoch, evaluate
from samplers import RASampler
import models
import utils
# --------------------------------------------

def get_args_parser():
    parser = argparse.ArgumentParser('ConViT training and evaluation script', add_help=False)
    parser.add_argument('--batch-size', default=64, type=int)
    parser.add_argument('--epochs', default=300, type=int)

    # Model parameters
    parser.add_argument('--model', default='convit_small', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--pretrained', action='store_true')

    parser.add_argument('--input-size', default=224, type=int, help='images input size')
    parser.add_argument('--embed_dim', default=48, type=int, help='embedding dimension per head')

    parser.add_argument('--drop', type=float, default=0.0, metavar='PCT',
                        help='Dropout rate (default: 0.)')
    parser.add_argument('--drop-path', type=float, default=0.1, metavar='PCT',
                        help='Drop path rate (default: 0.1)')
    parser.add_argument('--drop-block', type=float, default=None, metavar='PCT',
                        help='Drop block rate (default: None)')

    parser.add_argument('--model-ema', action='store_true')
    parser.add_argument('--no-model-ema', action='store_false', dest='model_ema')
    parser.set_defaults(model_ema=False)
    parser.add_argument('--model-ema-decay', type=float, default=0.99996, help='')
    parser.add_argument('--model-ema-force-cpu', action='store_true', default=False, help='')

    # Optimizer parameters
    parser.add_argument('--opt', default='adamw', type=str, metavar='OPTIMIZER',
                        help='Optimizer (default: "adamw"')
    parser.add_argument('--opt-eps', default=1e-8, type=float, metavar='EPSILON',
                        help='Optimizer Epsilon (default: 1e-8)')
    parser.add_argument('--opt-betas', default=None, type=float, nargs='+', metavar='BETA',
                        help='Optimizer Betas (default: None, use opt default)')
    parser.add_argument('--clip-grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--weight-decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')
    # Learning rate schedule parameters
    parser.add_argument('--sched', default='cosine', type=str, metavar='SCHEDULER',
                        help='LR scheduler (default: "cosine"')
    parser.add_argument('--lr', type=float, default=5e-4, metavar='LR',
                        help='learning rate (default: 5e-4)')
    parser.add_argument('--lr-noise', type=float, nargs='+', default=None, metavar='pct, pct',
                        help='learning rate noise on/off epoch percentages')
    parser.add_argument('--lr-noise-pct', type=float, default=0.67, metavar='PERCENT',
                        help='learning rate noise limit percent (default: 0.67)')
    parser.add_argument('--lr-noise-std', type=float, default=1.0, metavar='STDDEV',
                        help='learning rate noise std-dev (default: 1.0)')
    parser.add_argument('--warmup-lr', type=float, default=1e-6, metavar='LR',
                        help='warmup learning rate (default: 1e-6)')
    parser.add_argument('--min-lr', type=float, default=1e-5, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0 (1e-5)')

    parser.add_argument('--decay-epochs', type=float, default=30, metavar='N',
                        help='epoch interval to decay LR')
    parser.add_argument('--warmup-epochs', type=int, default=5, metavar='N',
                        help='epochs to warmup LR, if scheduler supports')
    parser.add_argument('--cooldown-epochs', type=int, default=10, metavar='N',
                        help='epochs to cooldown LR at min_lr, after cyclic schedule ends')
    parser.add_argument('--patience-epochs', type=int, default=10, metavar='N',
                        help='patience epochs for Plateau LR scheduler (default: 10')
    parser.add_argument('--decay-rate', '--dr', type=float, default=0.1, metavar='RATE',
                        help='LR decay rate (default: 0.1)')

    # Augmentation parameters
    parser.add_argument('--color-jitter', type=float, default=0.4, metavar='PCT',
                        help='Color jitter factor (default: 0.4)')
    parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                        help='Use AutoAugment policy. "v0" or "original". " + \
                             "(default: rand-m9-mstd0.5-inc1)'),
    parser.add_argument('--smoothing', type=float, default=0.1, help='Label smoothing (default: 0.1)')
    parser.add_argument('--train-interpolation', type=str, default='bicubic',
                        help='Training interpolation (random, bilinear, bicubic default: "bicubic")')

    parser.add_argument('--repeated-aug', action='store_true')
    parser.add_argument('--no-repeated-aug', action='store_false', dest='repeated_aug')
    parser.set_defaults(repeated_aug=True)

    # * Random Erase params
    parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                        help='Random erase prob (default: 0.25)')
    parser.add_argument('--remode', type=str, default='pixel',
                        help='Random erase mode (default: "pixel")')
    parser.add_argument('--recount', type=int, default=1,
                        help='Random erase count (default: 1)')
    parser.add_argument('--resplit', action='store_true', default=False,
                        help='Do not random erase first (clean) augmentation split')

    # * Mixup params
    parser.add_argument('--mixup', type=float, default=0.8,
                        help='mixup alpha, mixup enabled if > 0. (default: 0.8)')
    parser.add_argument('--cutmix', type=float, default=1.0,
                        help='cutmix alpha, cutmix enabled if > 0. (default: 1.0)')
    parser.add_argument('--cutmix-minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup-prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup-switch-prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup-mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # Dataset parameters
    parser.add_argument('--data-path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')
    parser.add_argument('--data-set', default='IMNET', choices=['CIFAR10', 'CIFAR100', 'IMNET', 'INAT', 'INAT19'],
                        type=str, help='Image Net dataset path')
    parser.add_argument('--sampling_ratio', default=1.,
                        type=float, help='fraction of samples to keep in the training set of imagenet')
    parser.add_argument('--nb_classes', default=None,
                        type=int, help='number of classes in imagenet')
    parser.add_argument('--inat-category', default='name',
                        choices=['kingdom', 'phylum', 'class', 'order', 'supercategory', 'family', 'genus', 'name'],
                        type=str, help='semantic granularity')

    parser.add_argument('--output_dir', default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--save_every', default=None, type=int, help='save model every epochs')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true', help='Perform evaluation only')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin-mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no-pin-mem', action='store_false', dest='pin_mem',
                        help='')
    parser.set_defaults(pin_mem=True)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')

    # locality parameters
    parser.add_argument('--local_up_to_layer', default=10, type=int,
                        help='number of GPSA layers')
    parser.add_argument('--locality_strength', default=1., type=float,
                        help='Determines how focused each head is around its attention center')

    # parameters for image embedding
    parser.add_argument('--image_embed_dim', default=128, type=int,
                        help='Dimension of the image embedding')
    
    # evealuation parameters
    parser.add_argument('--eval_every_x_epochs', default=1, type=int,
                        help='Evaluate every x epochs')
    
    return parser

class VGGFace2Dataset(datasets.DatasetFolder):
    def __init__(self, root, transform=None, target_transform=None):
        super(VGGFace2Dataset, self).__init__(
            root,
            loader=datasets.folder.default_loader,  # Use the default image loader
            extensions=('jpg', 'jpeg', 'png'),     # Accept jpg, jpeg, and png image files
            transform=transform,
            target_transform=target_transform,
            is_valid_file=None
        )


def create_batch(dataset, batch_size, shuffle=True):
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, num_workers=0)
    return dataloader

def build_transform(is_train, args):
    """Builds a transform for the given dataset. 
    A transform is a callable that takes an image and returns a transformed image.*
    
    Args:
        is_train (bool): True if the transform is for training, False otherwise.
        args (argparse.Namespace): The command line arguments.

    Returns:
        transform (callable): The transform.
    """
    resize_im = args.input_size > 32
    if is_train:
        # this should always dispatch to transforms_imagenet_train
        transform = create_transform(
            input_size=args.input_size,
            is_training=True,
            color_jitter=args.color_jitter,
            auto_augment=args.aa,
            interpolation=args.train_interpolation,
            re_prob=args.reprob,
            re_mode=args.remode,
            re_count=args.recount,
        )
        if not resize_im:
            # replace RandomResizedCropAndInterpolation with
            # RandomCrop
            transform.transforms[0] = transforms.RandomCrop(
                args.input_size, padding=4)
        return transform


class CustomContrastiveLoss(nn.Module):
    """/!\ placeholder for a custom loss function

    Args:
        nn (module): PyTorch module
    """
    def __init__(self, margin):
        super(CustomContrastiveLoss, self).__init__()
        self.margin = margin

    def forward(self, feature_vectors, labels):
        pairwise_distances = torch.cdist(feature_vectors, feature_vectors)
        label_matrix = labels.view(-1, 1) == labels.view(1, -1)

        # Loss for positive pairs (same person)
        positive_distances = pairwise_distances * label_matrix.float()
        positive_loss = torch.sum(positive_distances) / (torch.sum(label_matrix) - len(labels))

        # Loss for negative pairs (different people)
        negative_distances = pairwise_distances * (~label_matrix).float()
        negative_loss = torch.sum(torch.clamp(self.margin - negative_distances, min=0)) / torch.sum(~label_matrix)

        # Total loss
        loss = positive_loss + negative_loss
        return loss


def main(args):
    # main(args): This is the main function of the program. It takes a bunch of settings called
    # "args" as input and trains a model using those settings. It initializes distributed training,
    # builds the training and validation datasets, creates the model, sets up the optimizer and
    # loss function, and then trains the model for the specified number of epochs. After training,
    # it evaluates the model on the validation dataset and prints out the performance metrics.
    utils.init_distributed_mode(args)
    
    print(args)
    
    device = torch.device(args.device)
    
    # Fix the random seed for reproducibility. Add the rank of the process to the seed
    # so that each process has a different seed, ensuring that each process receives
    # different data samples during distributed training.
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    
    cudnn.benchmark = True
    
    transform_train = build_transform(True, args)
    transform_val = build_transform(False, args)
    
    dataset_train = VGGFace2Dataset(args.data_path, transform=transform_train)
    dataset_val = VGGFace2Dataset(args.data_path, transform=transform_val)
    # Set up the sampler for distributed training, which ensures that each process receives
    # different data samples during training. Use the Random Augmented Sampler if the
    # "repeated_aug" argument is set to True, otherwise use the Distributed Sampler.
    if True:  # args.distributed:
        num_tasks = utils.get_world_size()
        global_rank = utils.get_rank()
        if args.repeated_aug:
            sampler_train = RASampler(
                dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
            )
        else:
            sampler_train = torch.utils.data.DistributedSampler(
                dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
            )
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset_train)
    
    # Create the data loaders for the training and validation datasets.
    data_loader_train = torch.utils.data.DataLoader(
        dataset_train, sampler=sampler_train,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        pin_memory=args.pin_mem,
        drop_last=True,
    )
    
    data_loader_val = torch.utils.data.DataLoader(
        dataset_val, batch_size=int(1.5 * args.batch_size),
        shuffle=False, num_workers=args.num_workers,
        pin_memory=args.pin_mem, drop_last=False
    )
    
    # Initialize the Mixup function if mixup, cutmix, or cutmix_minmax is enabled.
    mixup_fn = None
    mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    if mixup_active:
        mixup_fn = Mixup(
            mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
            prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
            label_smoothing=args.smoothing, num_classes=args.nb_classes)
    
    # Create the model with the specified architecture and settings.
    print(f"Creating model: {args.model}")
    model = create_model(
        args.model,
        pretrained=args.pretrained,
        num_classes=args.image_embed_dim,
        drop_rate=args.drop,
        drop_path_rate=args.drop_path,
        drop_block_rate=args.drop_block,
        local_up_to_layer=args.local_up_to_layer,
        locality_strength=args.locality_strength,
        embed_dim = args.embed_dim,
    )
    
    print(model)
    model.to(device)
    
    # Set up the Exponential Moving Average (EMA) model if the "model_ema" argument is set to True.
    # default is False
    model_ema = None
    if args.model_ema:
        model_ema = ModelEma(
            model,
            decay=args.model_ema_decay,
            device='cpu' if args.model_ema_force_cpu else '',
            resume='')
    
    # Prepare the model for distributed training, if necessary.
    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module
    
    # Calculate the number of trainable parameters in the model and print it.
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)
    
    # Calculate the linearly scaled learning rate based on the batch size and world size.
    linear_scaled_lr = args.lr * args.batch_size * utils.get_world_size() / 512.0
    args.lr = linear_scaled_lr
    
    # Create the optimizer with the updated learning rate and model parameters.
    optimizer = create_optimizer(args, model)

    # Initialize the loss scaler for mixed precision training.
    loss_scaler = NativeScaler()
    
    # Create the learning rate scheduler.
    lr_scheduler, _ = create_scheduler(args, optimizer)
    
    # Set up the loss function (criterion) for training. Use SoftTargetCrossEntropy if mixup is enabled,
    # LabelSmoothingCrossEntropy if label smoothing is enabled, or CrossEntropyLoss otherwise.
    criterion = CustomContrastiveLoss()
    
    # Save the arguments to the output directory.
    output_dir = Path(args.output_dir)
    torch.save(args, output_dir / "args.pyT")
    
    # Resume training from a checkpoint if the "resume" argument is specified.
    if args.resume:
        if str(args.resume).startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')

        model_without_ddp.load_state_dict(checkpoint['model'])
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1
            if args.model_ema:
                utils._load_checkpoint_for_ema(model_ema, checkpoint['model_ema'])
    
    # If the "eval" argument is set to True, evaluate the model on the validation dataset and return.
    # /!\ this part needs checking
    if args.eval:
        throughput = utils.compute_throughput(model, resolution=args.input_size)
        print(f"Throughput : {throughput:.2f}")
        test_stats = evaluate(data_loader_val, model, device)
        print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")
        return
    
    
    
    # Start the training process and record the start time.
    print("Start training")
    start_time = time.time()
    max_accuracy = 0.0
    
    # Loop through each epoch (one complete run through the training dataset).
    for epoch in range(args.start_epoch, args.epochs):
        # Clean up unused memory.
        gc.collect()
        
        # If using distributed training, set the current epoch for the sampler.
        if args.distributed:
            data_loader_train.sampler.set_epoch(epoch)
        
        # Train the model for one epoch.
        train_stats = train_one_epoch(
            model, criterion, data_loader_train,
            optimizer, device, epoch, loss_scaler,
            args.clip_grad, model_ema, mixup_fn
        )
        
        # Update the learning rate scheduler for the current epoch.
        lr_scheduler.step(epoch)
        
        # Save the model's progress (checkpoints) to the output directory.
        if args.output_dir:
            checkpoint_paths = [output_dir / 'checkpoint.pth']
            # Save additional checkpoints based on the 'save_every' setting.
            if args.save_every is not None:
                if epoch % args.save_every == 0:
                    checkpoint_paths.append(output_dir / 'checkpoint_{}.pth'.format(epoch))
            # Save checkpoints to disk.
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'model_ema': get_state_dict(model_ema) if model_ema else None,
                    'args': args,
                }, checkpoint_path)
        
        if epoch % args.eval_every == 0 or epoch == args.epochs - 1:
            
            print("Start validation")
            # Evaluate the model on the validation dataset.
            test_stats = evaluate(data_loader_val, model, device)
            
            # Print the model's accuracy on the validation dataset.
            print(f"Accuracy of the network on the {len(dataset_val)} test images: {test_stats['acc1']:.1f}%")

            # Update the maximum accuracy achieved so far.
            max_accuracy = max(max_accuracy, test_stats["acc1"])
            print(f'Max accuracy: {max_accuracy:.2f}%')
            print("End validation")
            
        # Compute nonlocality, gating parameters, and distances for the model.
        nonlocality = {}
        gating_params = {}
        distances = {}
        batch = next(iter(data_loader_val))[0]
        batch = batch.to(device)
        batch = model_without_ddp.patch_embed(batch)
        for l in range(len(model_without_ddp.blocks)):
            attn = model_without_ddp.blocks[l].attn
            nonlocality[l] = attn.get_attention_map(batch).detach().cpu().numpy().tolist()
            if 'convit' in args.model and l < args.local_up_to_layer:
                p = attn.pos_proj.weight
                span = -1 / p.data[:, -1]
                dist_x = p.data[:, 0] * span / 2
                dist_y = p.data[:, 1] * span / 2
                dist = (dist_x ** 2 + dist_y ** 2) ** .5
                distances[l] = dist.cpu().numpy().tolist()
                gating_params[l] = attn.gating_param.data.cpu().numpy().tolist()

        # Print the collected statistics.
        print(log_stats)

        # Save the statistics to a log file if an output directory is specified.
        if args.output_dir and utils.is_main_process():
            with (output_dir / "log.txt").open("a") as f:
                f.write(json.dumps(log_stats) + "\n")
        
        # Calculate the total training time.
        total_time = time.time() - start_time

        # Convert the training time to a readable format.
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))

        # Print the total training time.
        print('Training time {}'.format(total_time_str))
        
    # Calculate the total training time after the training loop.
    total_time = time.time() - start_time

    # Convert the training time to a readable format.
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))

    # Print the total training time.
    print('Training time {}'.format(total_time_str))


def test_main_function():
    parser = get_args_parser()
    default_args = parser.parse_args(args=[])
    default_values = {key: value for key, value in vars(default_args).items()}
    
    # Update default values if necessary
    default_values['data_path'] = 'D:/face dataset/convit-main/empty_data'
    default_values['epochs'] = 1

    # Create a new Namespace object with updated default values
    args = argparse.Namespace(**default_values)
    print(args)
    
    main(args)

test_main_function()


# if __name__ == "__main__":
#     parser = argparse.ArgumentParser('ConViT training and evaluation script', parents=[get_args_parser()])
#     args = parser.parse_args()
#     main(args)
